#!/usr/bin/env python3
"""Flash individual partitions over Pantech's PDL download protocol (dev convenience).

  pdl_tool.py list   [--gpt gpt_main0.bin]
  pdl_tool.py flash  <label>=<file> [<label>=<file> ...] [--gpt gpt_main0.bin] [--dry-run] [--no-reboot]

  e.g.  pdl_tool.py flash aboot=emmc_appsboot.mbn
        pdl_tool.py flash boot=out/boot.img recovery=out/recovery.img

For iterating on a custom ROM: write one or a few raw partition images straight to the
phone in PDL/emergency-download mode, instead of building and uploading a whole PDL each time.

This cooperates with the device's own verification instead of defeating it:
  - it sends the CORRECT per-section checksum, so the bootloader's RAM pre-check and its
    read-back-from-eMMC check both run and must pass (a bad transfer is rejected);
  - it does NOT send a new partition table, so the device keeps its existing GPT (no repartition,
    no EFS/userdata wipe) -- the bootloader is designed to keep its GPT when none is sent;
  - it does not touch secure-boot state. On a secure target the bootloader still validates the
    signed image at boot exactly as before; this tool only transports bytes to a partition.

Protocol (from the device side: bootable/bootloader/lk/app/pdl, dloadmain.c/dloadpdl.c):
  USB 10a9:1104 "SKY USB MODEM", one interface, 2 bulk endpoints.
  Enter the mode first:  adb reboot oem-33   (or the hardware PDL key combo).
  12-byte AT command "AT*PDL*START" starts the download protocol; the device then
  exchanges 128-byte request / 16-byte response packets:
     DL_START, then per partition: DL_SECTION_START, DL_WRITE..., DL_SECTION_FINISH, then DL_FINISH.
  section_id = little-endian u32 of the GPT entry's unique-GUID first 4 bytes (= GPT index here).
  checksum   = sum of the image's little-endian u32 words.

Sparse images (system/userdata) are NOT handled here -- use pdltool.py repack / QFIL for those.
Needs pyusb and raw USB access (run with sudo, or install a udev rule for 10a9:1104).
"""
import argparse, array, os, struct, sys, time

VID, PID = 0x10A9, 0x1104
CHUNK = 0x80000                       # per-packet payload; divides the device's 15MB flush buffer
SPARSE_MAGIC = b'\x3a\xff\x26\xed'
AT_START = b'AT*PDL*START'            # 12 bytes
BLOCK = 512

# dloadpdl_req_header_type (128 bytes) / dloadpdl_rsp_header_type (16 bytes)
REQ = struct.Struct('<HBBI12sQQQQQIHIHIH50s')
RSP = struct.Struct('<HHIII')
assert REQ.size == 128 and RSP.size == 16
# section_data_type returned by DL_PARTITION_DUMP_START / _FINISH (56 bytes)
SECTION_DATA = struct.Struct('<I12sQQIQIII')
# phoneinfo_type stored at offset 0 of the "phoneinfo" partition, after a 32-byte magic (256 bytes)
PHONEINFO = struct.Struct('<I16s16sII16s16sI16sII72sI15s7s8sI2s4s8s28s')
assert SECTION_DATA.size == 56 and PHONEINFO.size == 256
PHONE_MAGIC = b'$PDL_PHONE_INFO_MAGIC$'
MAX_DUMP = 0x8000                     # device's MAX_DUMP_DATA_SIZE: per-chunk read-back size

DL_START, DL_FINISH, DL_SECTION_START, DL_SECTION_FINISH, DL_ERASE, DL_WRITE = 0, 1, 2, 3, 4, 5
DL_PARTITION_DUMP_START, DL_PARTITION_DUMP, DL_PARTITION_DUMP_FINISH = 41, 42, 43
CMD_NAME = {0: 'START', 1: 'FINISH', 2: 'SECTION_START', 3: 'SECTION_FINISH', 4: 'ERASE', 5: 'WRITE',
            41: 'DUMP_START', 42: 'DUMP', 43: 'DUMP_FINISH'}
ERRORS = {0: 'VERSION_MISMATCH', 4: 'ERASE_ERROR', 5: 'WRITE_ERROR', 6: 'MEMORY_ALLOC_FAIL',
          9: 'UPDATE_PART_TBL_FAIL', 13: 'SET_STATUS_START_FAIL', 14: 'CLEAR_STATUS_START_FAIL',
          110: 'SECTION_CRC_FAIL: data read back from eMMC does not match (bad flash)',
          111: 'SECTION_CRC_READ_ERROR', 116: 'SECTION_CRC_PRECHECK_FAIL: checksum mismatch in RAM'}

DEFAULT_GPT = 'gpt_main0.bin'


def die(m):
    sys.exit('error: ' + m)


def sum32(b, acc=0):
    if len(b) % 4:
        b = bytes(b) + b'\0' * (4 - len(b) % 4)
    return (acc + sum(array.array('I', b))) & 0xffffffff


# ------------------------------------------------------------------ GPT (device's own layout)
def read_gpt(path):
    import zlib
    with open(path, 'rb') as f:
        d = f.read(0x8700)
    if d.startswith(b'$PDL_PHONE_INFO_MAGIC$'):
        hdr, ents = d[0x234:0x434], d[0x434:0x4434]           # PDL header / phoneinfo image
    elif d[0x200:0x208] == b'EFI PART':
        hdr, ents = d[0x200:0x400], d[0x400:0x4400]           # gpt_main0.bin / disk dump
    elif d[:8] == b'EFI PART':
        hdr, ents = d[0:0x200], d[0x200:0x4200]
    else:
        die(f'{path}: no GPT (expected gpt_main0.bin, a PDL file or phoneinfo.bin)')
    h2 = bytearray(hdr[:0x5c]); h2[16:20] = b'\0' * 4
    if zlib.crc32(h2) != struct.unpack_from('<I', hdr, 16)[0]:
        die(f'{path}: GPT header CRC bad')
    nent, esz = struct.unpack_from('<II', hdr, 80)
    parts = {}
    for i in range(nent):
        e = ents[i * esz:(i + 1) * esz]
        if not any(e[:16]):
            continue
        sid, = struct.unpack_from('<I', e, 16)                # unique-GUID[0:4] == section_id
        s, en = struct.unpack_from('<QQ', e, 32)
        name = e[56:128].decode('utf-16le').split('\0')[0]
        parts[name] = (sid, s, (en - s + 1) * BLOCK)
    return parts


# ------------------------------------------------------------------ USB transport
class PDL:
    def __init__(self, verbose=False):
        import usb.core, usb.util
        self.core, self.util, self.verbose = usb.core, usb.util, verbose
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            die(f'device {VID:04x}:{PID:04x} not found. Put the phone in PDL mode '
                '(adb reboot oem-33) and check the USB cable.')
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except Exception:
            pass
        try:
            dev.set_configuration()
        except Exception as e:
            die(f'set_configuration failed ({e}). Try running with sudo.')
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]
        self.epout = self.epin = None
        for ep in intf:
            if self.util.endpoint_direction(ep.bEndpointAddress) == self.util.ENDPOINT_IN:
                self.epin = ep
            else:
                self.epout = ep
        if not self.epin or not self.epout:
            die('could not find both bulk endpoints')
        if verbose:
            print(f'  [usb] interface 0: IN ep {self.epin.bEndpointAddress:#04x}, '
                  f'OUT ep {self.epout.bEndpointAddress:#04x}')
        self.dev = dev

    def _send(self, data, timeout=20000):
        self.epout.write(bytes(data), timeout=timeout)

    def _read(self, timeout, n=512):
        try:
            return bytes(self.epin.read(n, timeout=timeout))
        except self.core.USBError as e:
            if getattr(e, 'errno', None) == 110 or 'timeout' in str(e).lower():
                return None
            raise

    def start_mode(self):
        # The first bulk OUT after configuration is dropped by the device, and it only
        # answers from a later AT*PDL*START. Resend until the banner comes back. This is
        # safe: a dropped AT does not enter the download loop, so no byte-stream desync.
        for attempt in range(1, 11):
            if self.verbose:
                print(f'  [usb] >> AT*PDL*START (try {attempt})')
            try:
                self._send(AT_START, timeout=3000)
            except self.core.USBError:           # stuck OUT endpoint: unstick and retry (AT is idempotent)
                try:
                    self.util.clear_halt(self.dev, self.epout.bEndpointAddress)
                except Exception:
                    pass
                continue
            b = self._read(2000)
            if self.verbose and b is not None:
                print(f'  [usb] << {b!r}')
            if b and b'PDL*START' in b:
                while self._read(400):          # drain anything extra
                    pass
                return
        die('no PDL banner after 10 tries. Re-enter PDL mode fresh '
            '(adb reboot oem-33 / key combo) and run again.')

    def command(self, cmd, section_id=0, name=b'', length=0, start=0, end=0,
                checksum=0, data=b'', flag=0):
        hdr = REQ.pack(cmd, 0, flag, section_id, name[:12].ljust(12, b'\0'),
                       length, start, end, len(data), length, checksum,
                       0, 0, 0, 0, 0, b'\0' * 50)
        if self.verbose:
            print(f'  [usb] >> {CMD_NAME.get(cmd, cmd)} (hdr 128 + data {len(data)})')
        self._send(hdr + data)
        deadline = time.time() + 30
        while time.time() < deadline:
            r = self._read(20000)
            if r is None:
                continue
            if r[:2] == b'AT':                 # stray banner, skip
                if self.verbose:
                    print(f'  [usb] << (skip banner {r!r})')
                continue
            if len(r) < RSP.size:
                if self.verbose:
                    print(f'  [usb] << short {len(r)}B {r.hex()}')
                continue
            cmd_r, ack, err, dlen, _chk = RSP.unpack(r[:RSP.size])
            if self.verbose:
                print(f'  [usb] << cmd={cmd_r} ack={ack} err={err} dlen={dlen}')
            if dlen:
                self._read(20000)
            if ack != 0:
                break
            return
        else:
            die(f'no response to {CMD_NAME.get(cmd, cmd)} within timeout. '
                'Re-run with --verbose and paste the output.')
        die(f'{CMD_NAME.get(cmd, cmd)} rejected by device: '
            f'{ERRORS.get(err, "error code %d" % err)}')

    def finish(self):
        # DL_FINISH: the device answers, then erases DDR and reboots -- tolerate the
        # USB disconnect that follows instead of treating it as an error.
        hdr = REQ.pack(DL_FINISH, 0, 0, 0, b'\0' * 12, 0, 0, 0, 0, 0, 0,
                       0, 0, 0, 0, 0, b'\0' * 50)
        if self.verbose:
            print('  [usb] >> FINISH')
        try:
            self._send(hdr)
            r = self._read(5000)
            if self.verbose:
                print(f'  [usb] << {r!r} (None is fine: device already rebooting)')
        except Exception as e:
            if self.verbose:
                print(f'  [usb] FINISH disconnect tolerated: {e}')

    # -- reading back from the phone (DL_PARTITION_DUMP) -----------------------
    def _recv_resp(self, timeout=20000):
        """Read one response: 16-byte header, then header.data_length bytes of payload.
        The device sends the header and payload as one contiguous bulk stream."""
        resp = bytearray()
        deadline = time.time() + 60
        while len(resp) < RSP.size:
            if time.time() > deadline:
                die('dump: no response header (re-run in PDL mode)')
            b = self._read(timeout, RSP.size + MAX_DUMP)
            if b is None:
                continue
            if not resp and b[:2] == b'AT':          # stray AT banner, ignore
                continue
            resp += b
        _cmd, ack, err, dlen, chk = RSP.unpack(bytes(resp[:RSP.size]))
        while len(resp) < RSP.size + dlen:
            if time.time() > deadline:
                die('dump: response truncated by device')
            b = self._read(timeout, RSP.size + MAX_DUMP)
            if b is None:
                continue
            resp += b
        return ack, err, chk, bytes(resp[RSP.size:RSP.size + dlen])

    def _dump_hdr(self, cmd, sid, flag=0, length=0, start=0, total=0):
        return REQ.pack(cmd, 0, flag, sid, b'\0' * 12,
                        length, start, start + length, 0, total, 0,
                        0, 0, 0, 0, 0, b'\0' * 50)

    def dump_start(self, sid, flag=0x02):
        """flag 0x02 = force a plain (non-sparse) raw dump. Returns section info or None."""
        if self.verbose:
            print(f'  [usb] >> DUMP_START sid {sid}')
        self._send(self._dump_hdr(DL_PARTITION_DUMP_START, sid, flag))
        ack, err, _chk, data = self._recv_resp()
        if ack != 0 or len(data) < SECTION_DATA.size:
            return None
        f = SECTION_DATA.unpack(data[:SECTION_DATA.size])
        return dict(sid=f[0], name=f[1].split(b'\0')[0].decode('latin1', 'replace'),
                    start=f[2], end=f[3], dump_size=f[4], total=f[5],
                    checksum=f[6], sparse_flag=f[7], sparse_checksum=f[8])

    def dump_chunk(self, sid, offset, length, total):
        self._send(self._dump_hdr(DL_PARTITION_DUMP, sid, 0, length, offset, total))
        ack, err, chk, data = self._recv_resp()
        if ack != 0:
            die(f'DUMP sid {sid} @ {offset:#x} rejected: {ERRORS.get(err, "err %d" % err)}')
        return data, chk

    def dump_finish(self, sid):
        self._send(self._dump_hdr(DL_PARTITION_DUMP_FINISH, sid))
        ack, err, _chk, data = self._recv_resp()
        if ack == 0 and len(data) >= SECTION_DATA.size:
            return SECTION_DATA.unpack(data[:SECTION_DATA.size])[6]   # total checksum
        return None

    def read_region(self, sid, offset, length):
        """Read `length` bytes starting at `offset` within a partition (for small reads
        like phoneinfo); does not dump the whole partition.
        The device's mmc_read needs 512-block-aligned offset and size, so round up."""
        if offset % BLOCK:
            die('read_region: offset must be 512-aligned')
        info = self.dump_start(sid, flag=0x02)
        if not info:
            die(f'dump_start NACK for section id {sid}')
        need = -(-length // BLOCK) * BLOCK        # round the read length up to a 512 block
        out = bytearray()
        off = offset
        while len(out) < need:
            want = min(MAX_DUMP, need - len(out), info['total'] - off)
            if want <= 0:
                break
            data, chk = self.dump_chunk(sid, off, want, info['total'])
            if sum32(data) != chk:
                die('read_region: checksum mismatch from device')
            out += data
            off += len(data)
        self.dump_finish(sid)
        return bytes(out[:length])


# ------------------------------------------------------------------ flashing
def prep_image(path):
    """Return (stored_size, checksum). Raw images only; pads to a 512-byte block."""
    with open(path, 'rb') as f:
        if f.read(4) == SPARSE_MAGIC:
            die(f'{path}: Android sparse image -- flash system/userdata with '
                'pdltool.py repack + the Pantech PDL tool, or QFIL, not this tool.')
    sz = os.path.getsize(path)
    stored = -(-sz // BLOCK) * BLOCK
    c = done = 0
    with open(path, 'rb') as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            c = sum32(b, c); done += len(b)
    if stored != sz:                           # last partial block padded with zeros
        c = sum32(b'\0' * (stored - sz), c)
    return stored, c


def plan(jobs, parts):
    out = []
    for label, path in jobs:
        if not os.path.exists(path):
            die(f'{path}: no such file')
        if label not in parts:
            die(f'{label}: not a partition on this device. known: ' + ', '.join(sorted(parts)))
        sid, _, pbytes = parts[label]
        stored, ck = prep_image(path)
        if stored > pbytes:
            die(f'{label}: {stored:#x} bytes does not fit partition ({pbytes:#x})')
        out.append(dict(label=label, path=path, sid=sid, size=stored, ck=ck, pbytes=pbytes))
    return out


def cmd_list(a):
    parts = read_gpt(a.gpt)
    print(f'{"partition":14} {"section_id":>10} {"size":>14}')
    for name, (sid, s, pbytes) in sorted(parts.items(), key=lambda x: x[1][1]):
        print(f'{name:14} {sid:>10} {pbytes:>14,}')


def cmd_flash(a):
    jobs = []
    for tok in a.assignments:
        if '=' not in tok:
            die(f'bad argument "{tok}", expected label=file')
        lab, fn = tok.split('=', 1)
        jobs.append((lab, fn))
    parts = read_gpt(a.gpt)
    items = plan(jobs, parts)

    print('plan (device verification stays ON):')
    for it in items:
        print(f'  {it["label"]:12} <- {it["path"]:28} {it["size"]:#11x} '
              f'(sid {it["sid"]}, ck {it["ck"]:#010x})')
    if a.dry_run:
        print('dry-run: no USB, nothing sent.')
        return
    if not a.yes:
        if input('proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
            die('aborted')

    if a.boot == 'recovery' and 'misc' not in parts:
        die('--boot recovery needs a "misc" partition in the GPT')

    pdl = PDL(verbose=a.verbose)
    pdl.start_mode()
    pdl.command(DL_START)
    for it in items:
        name = it['label'].encode()
        pdl.command(DL_SECTION_START, it['sid'], name, length=it['size'])
        off, size = 0, it['size']
        with open(it['path'], 'rb') as f:
            while off < size:
                want = min(CHUNK, size - off)
                b = f.read(want)
                if len(b) < want:                       # pad final block to the stored size
                    b = b + b'\0' * (want - len(b))
                pdl.command(DL_WRITE, it['sid'], name, start=off, end=off + want, data=b)
                off += want
                print(f'\r  {it["label"]}: {off:#x}/{size:#x}', end='', flush=True)
        print()
        pdl.command(DL_SECTION_FINISH, it['sid'], name, length=it['size'], checksum=it['ck'])
        print('  ... verified OK')

    if a.boot == 'recovery':
        write_bcb_recovery(pdl, parts['misc'][0])
        print('misc BCB set to "boot-recovery"')

    if a.no_reboot and a.boot != 'recovery':
        print('left in PDL mode (--no-reboot); DL_FINISH not sent.')
    else:
        pdl.finish()                           # device reboots right after responding
        tgt = 'recovery' if a.boot == 'recovery' else 'normal'
        print(f'done; device erases scratch (DDR) and reboots ({tgt}).')


def write_bcb_recovery(pdl, misc_sid):
    """Write the bootloader control block so the next boot enters recovery.

    On eMMC this LK reads the BCB from offset 0 of 'misc'
    (app/aboot/recovery.c emmc_get_recovery_msg: mmc_read(ptn, in, blocksize)),
    struct recovery_message { char command[32]; ... }. command == "boot-recovery"
    sets boot_into_recovery. One 512-byte block at offset 0 is enough; the bootloader
    clears the command after reading it, so this is one-shot.
    """
    buf = bytearray(BLOCK)
    buf[0:13] = b'boot-recovery'
    pdl.command(DL_SECTION_START, misc_sid, b'misc', length=len(buf))
    pdl.command(DL_WRITE, misc_sid, b'misc', start=0, end=len(buf), data=bytes(buf))
    pdl.command(DL_SECTION_FINISH, misc_sid, b'misc', length=len(buf), checksum=sum32(buf))


def flash_one(pdl, sid, name, path, pbytes=None, verbose=False):
    """Flash one raw image to a partition inside an already-open PDL session."""
    stored, ck = prep_image(path)
    if pbytes is not None and stored > pbytes:
        die(f'{name}: {stored:#x} bytes does not fit partition ({pbytes:#x})')
    nm = name.encode()
    pdl.command(DL_SECTION_START, sid, nm, length=stored)
    off = 0
    with open(path, 'rb') as f:
        while off < stored:
            want = min(CHUNK, stored - off)
            b = f.read(want)
            if len(b) < want:
                b = b + b'\0' * (want - len(b))
            pdl.command(DL_WRITE, sid, nm, start=off, end=off + want, data=b)
            off += want
            print(f'\r    {name}: {off:#x}/{stored:#x}', end='', flush=True)
    print()
    pdl.command(DL_SECTION_FINISH, sid, nm, length=stored, checksum=ck)
    return stored, ck


def backup_partition(pdl, sid, name, out_path, verbose=False):
    """Read a whole partition back from the phone and write it to out_path.
    Verifies every chunk's checksum and the device's overall checksum."""
    info = pdl.dump_start(sid, flag=0x02)
    if not info:
        die(f'{name}: dump_start rejected (section id {sid} not on device?)')
    total = info['total']
    acc = off = 0
    with open(out_path, 'wb') as f:
        while off < total:
            length = min(MAX_DUMP, total - off)
            data, chk = pdl.dump_chunk(sid, off, length, total)
            if sum32(data) != chk:
                die(f'{name}: chunk checksum mismatch at {off:#x} (bad read)')
            f.write(data)
            acc = (acc + chk) & 0xffffffff
            off += len(data)
            print(f'\r    {name}: {off:#x}/{total:#x}', end='', flush=True)
    print()
    dev = pdl.dump_finish(sid)
    if dev is not None and dev != acc:
        die(f'{name}: device checksum {dev:#010x} != read {acc:#010x}')
    print(f'    verified OK (checksum {acc:#010x})')
    return total


def read_phoneinfo(pdl, sid):
    data = pdl.read_region(sid, 0, 0x120)
    if data[:len(PHONE_MAGIC)] != PHONE_MAGIC:
        return None
    f = PHONEINFO.unpack(data[32:32 + PHONEINFO.size])
    s = lambda b: b.split(b'\0')[0].decode('latin1', 'replace').strip()
    return dict(version=f[0], model=s(f[1]), binary=s(f[2]), fs_ver=f[3], nv_ver=f[4],
                build_date=s(f[5]), build_time=s(f[6]), bl_ver=f[7], efs_size=f[9],
                part_num=f[10], imei=f[13].hex(), secure_model=f[17].hex(), secure_msm=f[18].hex())


# ------------------------------------------------------------------ interactive menu
EFS_PARTS = ['modemst1', 'modemst2', 'fsg']


def _ask(prompt, choices):
    """Show a numbered menu, return the chosen key (0 is always a 'back/cancel' key)."""
    while True:
        print()
        for k, label in choices:
            print(f'  {k}) {label}')
        sel = input('선택> ').strip()
        if sel in dict(choices):
            return sel
        print('  잘못된 선택')


def _pick_partition(parts):
    """Let the user pick a partition by number. Returns name or None."""
    names = [n for n, _ in sorted(parts.items(), key=lambda x: x[1][1])]
    print()
    for i, n in enumerate(names, 1):
        sid, _s, pb = parts[n]
        print(f'  {i:>2}) {n:14} (sid {sid}, {pb:,} bytes)')
    print('   0) 뒤로')
    while True:
        sel = input('파티션 번호> ').strip()
        if sel == '0':
            return None
        if sel.isdigit() and 1 <= int(sel) <= len(names):
            return names[int(sel) - 1]
        print('  잘못된 번호')


def _adb_reboot(mode_arg, label):
    import subprocess
    print(f'\n[{label}] PDL 모드에서는 직접 설정할 수 없습니다 '
          '(리부팅 사유가 eMMC가 아니라 IMEM 레지스터에 저장됨).')
    print('일반 부팅 후 OS가 올라오면 adb로 해당 모드로 재부팅합니다. (adb/USB 디버깅 필요)')
    if input('진행할까요? [y/N] ').strip().lower() not in ('y', 'yes'):
        return False
    return mode_arg  # caller finishes PDL (normal boot) then runs adb


def _post_action(pdl, misc_sid):
    """After a flash/backup: offer normal reboot or return to the main menu."""
    sel = _ask('작업 완료. 다음 동작:', [
        ('1', '일반부팅으로 리붓'),
        ('2', '메인메뉴로 돌아가기'),
    ])
    if sel == '1':
        pdl.finish()
        print('일반부팅으로 리부팅합니다.')
        return True        # session ended (device rebooting)
    return False


def reboot_menu(pdl, parts):
    """Returns True if the device is being rebooted (session over), else False."""
    misc_sid = parts['misc'][0] if 'misc' in parts else None
    sel = _ask('리붓 메뉴:', [
        ('1', '일반부팅'),
        ('2', '리커버리부팅'),
        ('3', '부트로더(fastboot) 부팅'),
        ('4', 'EDL(9008) 부팅'),
        ('5', 'PDL 부팅'),
        ('0', '뒤로'),
    ])
    if sel == '0':
        return False
    if sel == '1':
        pdl.finish()
        print('일반부팅으로 리부팅합니다.')
        return True
    if sel == '2':
        if misc_sid is None:
            die('misc 파티션이 없어 리커버리 부팅을 설정할 수 없습니다')
        write_bcb_recovery(pdl, misc_sid)
        print('misc BCB="boot-recovery" 기록. 리커버리로 리부팅합니다.')
        pdl.finish()
        return True
    # bootloader / edl / pdl: not settable from PDL -> normal boot, then adb
    arg = {'3': ('bootloader', '부트로더'), '4': ('edl', 'EDL'), '5': ('oem-33', 'PDL')}[sel]
    if _adb_reboot(arg[0], arg[1]):
        pdl.finish()
        print('일반부팅 중... OS 부팅 후 adb로 전환합니다.')
        import subprocess
        try:
            subprocess.run(['adb', 'wait-for-device'], timeout=180, check=False)
            subprocess.run(['adb', 'reboot', arg[0]], timeout=30, check=False)
            print(f'adb reboot {arg[0]} 전송됨.')
        except Exception as e:
            print(f'adb 전환 실패: {e}\n부팅 후 직접 실행하세요:  adb reboot {arg[0]}')
        return True
    return False


def _print_device_header(info):
    """Show the phone info (read once at startup) right-aligned at the top of the menu."""
    import shutil
    w = shutil.get_terminal_size(fallback=(80, 24)).columns
    if info:
        lines = [
            f'[기기] 모델 {info["model"]}   바이너리 {info["binary"]}',
            f'빌드 {info["build_date"]} {info["build_time"]}   FS{info["fs_ver"]}/NV{info["nv_ver"]}  BL{info["bl_ver"]}',
            f'EFS {info["efs_size"]:,} ({info["part_num"]}개)   secure {info["secure_model"]} / {info["secure_msm"]}',
            f'IMEI {info["imei"]}',
        ]
    else:
        lines = ['[기기] phoneinfo 없음']
    print()
    for ln in lines:
        print(ln.rjust(w) if len(ln) < w else ln)
    print('─' * w)


def cmd_menu(a):
    parts = read_gpt(a.gpt)
    backupdir = a.backupdir or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup')
    os.makedirs(backupdir, exist_ok=True)         # all backups land here
    print('=== Pantech PDL 메뉴 (IM-100 / ef71s) ===')
    print(f'GPT: {a.gpt}   ({len(parts)}개 파티션)')
    print(f'백업 폴더: {backupdir}')
    print('\nPDL 모드 진입 중...')
    pdl = PDL(verbose=a.verbose)
    pdl.start_mode()
    pdl.command(DL_START)          # one download session; ends only on reboot
    print('연결됨.')
    info = read_phoneinfo(pdl, parts['phoneinfo'][0]) if 'phoneinfo' in parts else None

    while True:
        _print_device_header(info)
        sel = _ask('메인 메뉴:', [
            ('1', '파티션 플래시'),
            ('2', '파티션 백업'),
            ('3', 'EFS 백업/복원 (modemst1 / modemst2 / fsg)'),
            ('4', '리붓'),
        ])
        if sel == '1':
            name = _pick_partition(parts)
            if not name:
                continue
            sid, _s, pb = parts[name]
            path = input(f'{name} 에 올릴 파일 경로> ').strip().strip('"').strip("'")
            if not path or not os.path.exists(path):
                print('  파일 없음'); continue
            stored, ck = prep_image(path)
            print(f'  {name}: {path} ({stored:#x} bytes, checksum {ck:#010x})')
            if input('  플래시할까요? [y/N] ').strip().lower() not in ('y', 'yes'):
                continue
            flash_one(pdl, sid, name, path, pbytes=pb, verbose=a.verbose)
            print('  ... verified OK')
            if _post_action(pdl, parts.get('misc', (None,))[0]):
                return
        elif sel == '2':
            name = _pick_partition(parts)
            if not name:
                continue
            sid, _s, pb = parts[name]
            default = os.path.join(backupdir, f'{name}.img')
            path = input(f'저장 경로 [{default}]> ').strip().strip('"').strip("'") or default
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            backup_partition(pdl, sid, name, path, verbose=a.verbose)
            print(f'  저장됨: {path}')
            if _post_action(pdl, parts.get('misc', (None,))[0]):
                return
        elif sel == '3':
            sub = _ask('EFS:', [('1', 'EFS 백업 (3개 파티션 → 폴더)'),
                                 ('2', 'EFS 복원 (폴더 → 3개 파티션)'), ('0', '뒤로')])
            if sub == '0':
                continue
            missing = [p for p in EFS_PARTS if p not in parts]
            if missing:
                print('  GPT에 없음: ' + ', '.join(missing)); continue
            if sub == '1':
                os.makedirs(backupdir, exist_ok=True)
                for p in EFS_PARTS:
                    sid = parts[p][0]
                    out = os.path.join(backupdir, f'{p}.img')
                    print(f'  백업 {p} -> {out}')
                    backup_partition(pdl, sid, p, out, verbose=a.verbose)
                print(f'  EFS 백업 완료: {backupdir}')
            else:
                files = {p: os.path.join(backupdir, f'{p}.img') for p in EFS_PARTS}
                miss = [p for p, fp in files.items() if not os.path.exists(fp)]
                if miss:
                    print('  백업 파일 없음: ' + ', '.join(files[p] for p in miss)); continue
                print('  경고: EFS 복원은 모뎀/IMEI 영역을 덮어씁니다.')
                if input('  진행? [y/N] ').strip().lower() not in ('y', 'yes'):
                    continue
                for p in EFS_PARTS:
                    sid, _s, pb = parts[p]
                    print(f'  복원 {files[p]} -> {p}')
                    flash_one(pdl, sid, p, files[p], pbytes=pb, verbose=a.verbose)
                print('  EFS 복원 완료.')
            if _post_action(pdl, parts.get('misc', (None,))[0]):
                return
        elif sel == '4':
            if reboot_menu(pdl, parts):
                return


def cmd_backup(a):
    jobs = []
    for tok in a.assignments:
        if '=' not in tok:
            die(f'bad argument "{tok}", expected label=file')
        lab, fn = tok.split('=', 1)
        jobs.append((lab, fn))
    parts = read_gpt(a.gpt)
    for lab, _fn in jobs:
        if lab not in parts:
            die(f'{lab}: not a partition. known: ' + ', '.join(sorted(parts)))
    pdl = PDL(verbose=a.verbose)
    pdl.start_mode()
    pdl.command(DL_START)
    for lab, fn in jobs:
        os.makedirs(os.path.dirname(os.path.abspath(fn)) or '.', exist_ok=True)
        print(f'backup {lab} -> {fn}')
        backup_partition(pdl, parts[lab][0], lab, fn, verbose=a.verbose)
    if a.no_reboot:
        print('left in PDL mode (--no-reboot).')
    else:
        pdl.finish()
        print('done; device reboots (normal).')


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sp = ap.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('list'); p.add_argument('--gpt', default=DEFAULT_GPT); p.set_defaults(fn=cmd_list)
    p = sp.add_parser('flash')
    p.add_argument('assignments', nargs='+', metavar='label=file')
    p.add_argument('--gpt', default=DEFAULT_GPT, help='GPT source: gpt_main0.bin / a PDL / phoneinfo.bin')
    p.add_argument('--dry-run', action='store_true', help='build the plan and checksums, send nothing')
    p.add_argument('--no-reboot', action='store_true', help='stay in PDL mode after writing')
    p.add_argument('--boot', choices=('normal', 'recovery'), default='normal',
                   help='recovery: also write misc BCB so the phone reboots into recovery')
    p.add_argument('-v', '--verbose', action='store_true', help='log every USB packet (for debugging)')
    p.add_argument('-y', '--yes', action='store_true', help='do not prompt before writing')
    p.set_defaults(fn=cmd_flash)

    p = sp.add_parser('backup', help='read whole partitions back from the phone to files')
    p.add_argument('assignments', nargs='+', metavar='label=file')
    p.add_argument('--gpt', default=DEFAULT_GPT)
    p.add_argument('--no-reboot', action='store_true', help='stay in PDL mode after reading')
    p.add_argument('-v', '--verbose', action='store_true')
    p.set_defaults(fn=cmd_backup)

    p = sp.add_parser('menu', help='interactive single-session menu (flash / backup / efs / info / reboot)')
    p.add_argument('--gpt', default=DEFAULT_GPT)
    p.add_argument('--backupdir', default=None, help='folder for all backups (default: <tool dir>/backup)')
    p.add_argument('-v', '--verbose', action='store_true')
    p.set_defaults(fn=cmd_menu)

    a = ap.parse_args(); a.fn(a)


if __name__ == '__main__':
    main()
