import os
import time

from dbtriage import envfile
from dbtriage.envfile import EnvFile, parse


def test_parse():
    assert parse('# c\n\nA=1\nexport B = two\nC="q # not a comment"\nD=\'single\'\nE=x # comment\nbad line\n1X=no\n') == \
        {"A": "1", "B": "two", "C": "q # not a comment", "D": "single", "E": "x"}


def test_follows_file_but_real_environment_wins(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    monkeypatch.setenv("DBT_T_SOURCED", "same")        # sourced from the file before start: the file owns it
    monkeypatch.setenv("DBT_T_REAL", "from-systemd")    # set differently by the real environment: hands off
    monkeypatch.delenv("DBT_T_NEW", raising=False)
    f.write_text("DBT_T_SOURCED=same\nDBT_T_REAL=from-file\n")
    e = EnvFile(f)
    assert os.environ["DBT_T_REAL"] == "from-systemd" and "DBT_T_NEW" not in os.environ
    e.CHECK_EVERY_S = 0
    time.sleep(0.01)
    f.write_text("DBT_T_SOURCED=changed\nDBT_T_REAL=other\nDBT_T_NEW=added\n")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert sorted(e.refresh()) == ["DBT_T_NEW", "DBT_T_SOURCED"]
    assert os.environ["DBT_T_SOURCED"] == "changed" and os.environ["DBT_T_NEW"] == "added" and os.environ["DBT_T_REAL"] == "from-systemd"
    assert e.refresh() == []  # unchanged file: nothing to do
    f.write_text("DBT_T_SOURCED=changed\n")
    os.utime(f, (time.time() + 10, time.time() + 10))
    assert e.refresh() == ["DBT_T_NEW"] and "DBT_T_NEW" not in os.environ  # removed from the file -> removed
    monkeypatch.delenv("DBT_T_SOURCED", raising=False)


def test_init_finds_env_next_to_config(tmp_path, monkeypatch):
    (tmp_path / "targets.toml").write_text("")
    (tmp_path / ".env").write_text("DBT_T_INIT=yes\n")
    monkeypatch.delenv("DBT_T_INIT", raising=False)
    monkeypatch.delenv("DBTRIAGE_ENV_FILE", raising=False)
    monkeypatch.delenv("DBTRIAGE_NO_ENV_FILE", raising=False)
    assert envfile.init(tmp_path / "targets.toml").path == tmp_path / ".env" and os.environ["DBT_T_INIT"] == "yes"
    monkeypatch.setenv("DBTRIAGE_NO_ENV_FILE", "1")
    assert envfile.init(tmp_path / "targets.toml") is None
    monkeypatch.delenv("DBT_T_INIT", raising=False)
    envfile._active = None
