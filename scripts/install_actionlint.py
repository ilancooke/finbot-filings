"""Download a pinned, checksum-verified workflow linter into a specified directory."""
import argparse
import hashlib
from pathlib import Path
import platform
import tarfile
import tempfile
import urllib.request

VERSION = "1.7.12"
CHECKSUMS = {
    "darwin_arm64": "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    "linux_arm64": "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    "linux_amd64": "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    target = platform.system().lower() + "_" + arch
    filename = f"actionlint_{VERSION}_{target}.tar.gz"
    with urllib.request.urlopen(f"https://github.com/rhysd/actionlint/releases/download/v{VERSION}/{filename}", timeout=30) as response:
        content = response.read(10 * 1024 * 1024)
    if hashlib.sha256(content).hexdigest() != CHECKSUMS[target]:
        raise ValueError("actionlint archive checksum mismatch")
    args.directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as archive:
        archive.write(content)
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:gz") as tar:
            member = tar.extractfile("actionlint")
            if member is None:
                raise ValueError("archive is missing actionlint")
            executable = args.directory / "actionlint"
            executable.write_bytes(member.read())
            executable.chmod(0o755)
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
