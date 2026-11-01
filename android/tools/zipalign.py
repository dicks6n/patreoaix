#!/usr/bin/env python3
"""zipalign without the Android SDK: every uncompressed entry starts on a 4-byte boundary (native .so files on
4096), padded with the zip "extra" field like Google's zipalign. Run BEFORE signing (APK Signature Scheme v2 signs
the exact bytes, and the signer keeps the offsets).
    python3 zipalign.py in.apk out.apk     align
    python3 zipalign.py --check file.apk    exit 1 if any uncompressed entry is misaligned"""
import struct
import sys
import zipfile


def offsets(path):
    with open(path, 'rb') as f, zipfile.ZipFile(path) as z:
        for i in z.infolist():
            f.seek(i.header_offset)
            h = f.read(30)
            n, e = struct.unpack('<HH', h[26:30])
            yield i, i.header_offset + 30 + n + e


def need(name):
    return 4096 if name.endswith('.so') else 4


def check(path):
    bad = [(i.filename, off) for i, off in offsets(path) if i.compress_type == zipfile.ZIP_STORED and off % need(i.filename)]
    for name, off in bad:
        print(f'NOT ALIGNED {name} at {off}')
    print('zipalign check:', 'OK' if not bad else f'{len(bad)} misaligned')
    return not bad


def align(src, dst):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, 'w') as zout:
        for i in zin.infolist():
            data = zin.read(i.filename)
            zi = zipfile.ZipInfo(i.filename, date_time=i.date_time)
            zi.compress_type, zi.external_attr, zi.create_system = i.compress_type, i.external_attr, i.create_system
            zi.extra = b''
            if i.compress_type == zipfile.ZIP_STORED:
                start = zout.fp.tell() + 30 + len(i.filename.encode('utf-8'))
                zi.extra = b'\0' * ((-start) % need(i.filename))
            zout.writestr(zi, data)


if __name__ == '__main__':
    if sys.argv[1] == '--check':
        sys.exit(0 if check(sys.argv[2]) else 1)
    align(sys.argv[1], sys.argv[2])
    if not check(sys.argv[2]):
        sys.exit(1)
