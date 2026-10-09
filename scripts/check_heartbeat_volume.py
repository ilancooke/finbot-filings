"""Offline Docker check of image volume ownership; no runtime or AWS calls."""
import argparse
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    args = parser.parse_args()
    # An anonymous image-declared VOLUME exercises copy-up permissions, unlike tmpfs.
    probe = "import os,pathlib; assert os.getuid()==10001; p=pathlib.Path('/tmp/volume-probe'); p.write_bytes(b'ok'); p.unlink()"
    subprocess.run(["docker", "run", "--rm", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--entrypoint", "python",
        args.image, "-c", probe], check=True, timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
