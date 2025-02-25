from pathlib import Path
from subprocess import CalledProcessError

import pytest

from tests.conftest import GG_VALID_TOKEN
from tests.functional.utils import recreate_censored_content, run_ggshield
from tests.repository import Repository


def test_scan_prepush(tmp_path: Path) -> None:
    # GIVEN a remote repository
    remote_repo = Repository.create(tmp_path / "remote", bare=True)

    # AND a local clone
    local_repo = Repository.clone(remote_repo.path, tmp_path / "local")

    # AND ggshield installed as a pre-push hook
    run_ggshield("install", "-m", "local", "-t", "pre-push", cwd=local_repo.path)

    # AND a secret committed
    secret_file = local_repo.path / "secret.conf"
    secret_content = f"password = {GG_VALID_TOKEN}"
    secret_file.write_text(secret_content)
    local_repo.git("add", "secret.conf")
    local_repo.create_commit()

    # WHEN I try to push
    # THEN the hook prevents the push
    with pytest.raises(CalledProcessError) as exc:
        local_repo.git("push")

    # AND the error message contains the leaked secret
    stdout = exc.value.stdout.decode()
    assert recreate_censored_content(secret_content, GG_VALID_TOKEN) in stdout
