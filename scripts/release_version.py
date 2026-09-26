"""Choose the exact distribution version before building release artifacts."""
import sys

from packaging.version import Version


def release_version(base, branch, revision):
    if not branch.startswith('PR-'):
        return base
    # Normalize case, separators and numeric local segments just as the
    # build backend does, before writing metadata or constructing filenames.
    return str(Version(f'{base}+{branch}.{revision}'))


if __name__ == '__main__':
    print(release_version(*sys.argv[1:]))
