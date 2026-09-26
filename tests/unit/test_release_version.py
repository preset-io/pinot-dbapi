"""Publishing uses the same normalized version as distribution filenames."""
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'release_version.py'


@pytest.mark.parametrize('base,branch,revision,expected', [
    ('9.1.2.2', 'PR-5', 'e2bc688', '9.1.2.2+pr.5.e2bc688'),
    ('9.1.2.2', 'PR-5', 'ABCDEF1', '9.1.2.2+pr.5.abcdef1'),
    ('9.1.2.2', 'PR-5', '0123456', '9.1.2.2+pr.5.123456'),
    ('9.1.2.2', 'master', 'e2bc688', '9.1.2.2'),
    ('9.1.2.2', 'fix/reflected-result-types', 'e2bc688', '9.1.2.2'),
])
def test_release_version(base, branch, revision, expected):
    result = subprocess.check_output(
        [sys.executable, str(SCRIPT), base, branch, revision], text=True,
    )
    assert result.strip() == expected
