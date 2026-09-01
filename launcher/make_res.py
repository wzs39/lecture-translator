#!/usr/bin/env python3
"""Generate launcher/app.res for csc /win32res using the Windows SDK rc.exe.

Version numbers come from root version.txt; the icon from app.ico.

    python make_res.py
"""
import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RC = os.path.join(HERE, "app.rc")
OUT = os.path.join(HERE, "app.res")


def sdk_bases():
    bases = [os.environ.get("ProgramFiles(x86)"),
             r"C:\Program Files (x86)"]
    return {os.path.normpath(b) for b in bases if b}


def find_rc():
    for base in sdk_bases():
        for pattern in (
            os.path.join(base, "Windows Kits", "10", "bin", "*", "x64", "rc.exe"),
            os.path.join(base, "Windows Kits", "10", "bin", "*", "x86", "rc.exe"),
        ):
            hits = sorted(glob.glob(pattern))
            if hits:
                return hits[-1]
    return None


def find_include():
    for base in sdk_bases():
        hits = sorted(glob.glob(os.path.join(base, "Windows Kits", "10",
                                             "Include", "*", "um")))
        if hits:
            return os.path.normpath(hits[-1])
    return None


def main():
    ver = open(os.path.join(HERE, "..", "version.txt")).read().strip()
    parts = (ver.split(".") + ["0", "0", "0", "0"])[:4]
    fv = ",".join(p if p.isdigit() else "0" for p in parts)
    include = find_include()
    if include is None:
        print("Windows SDK include dir not found")
        return 1
    rc = find_rc()
    if rc is None:
        print("rc.exe not found - Windows SDK required")
        return 1
    includes = [include]
    shared = os.path.join(os.path.dirname(include), "shared")
    if os.path.isdir(shared):
        includes.append(shared)
    src = (
        "#include <windows.h>\n"
        "1 ICON \"app.ico\"\n"
        "1 VERSIONINFO\n"
        "FILEVERSION %s\n"
        "PRODUCTVERSION %s\n"
        "FILEFLAGSMASK 0x3fL\n"
        "FILEFLAGS 0x0L\n"
        "FILEOS VOS_NT_WINDOWS32\n"
        "FILETYPE VFT_APP\n"
        "FILESUBTYPE 0x0L\n"
        "BEGIN\n"
        "  BLOCK \"StringFileInfo\"\n"
        "  BEGIN\n"
        "    BLOCK \"040904B0\"\n"
        "    BEGIN\n"
        "      VALUE \"CompanyName\", \"wzs39\\0\"\n"
        "      VALUE \"FileDescription\", \"Lecture Translator desktop launcher\\0\"\n"
        "      VALUE \"FileVersion\", \"%s\\0\"\n"
        "      VALUE \"InternalName\", \"LectureTranslatorLauncher.exe\\0\"\n"
        "      VALUE \"LegalCopyright\", \"(c) 2026 wzs39\\0\"\n"
        "      VALUE \"OriginalFilename\", \"LectureTranslatorLauncher.exe\\0\"\n"
        "      VALUE \"ProductName\", \"Lecture Translator\\0\"\n"
        "      VALUE \"ProductVersion\", \"%s\\0\"\n"
        "    END\n"
        "  END\n"
        "  BLOCK \"VarFileInfo\"\n"
        "  BEGIN\n"
        "    VALUE \"Translation\", 0x409, 1200\n"
        "  END\n"
        "END\n"
    ) % (fv, fv, ver, ver)
    with open(RC, "w", newline="\r\n") as f:
        f.write(src)
    args = [rc, "/fo", OUT]
    for inc in includes:
        args += ["/I", inc]
    args.append(RC)
    subprocess.check_call(args)
    print("wrote %s (version %s, via rc.exe)" % (OUT, ver))
    return 0


if __name__ == "__main__":
    sys.exit(main())