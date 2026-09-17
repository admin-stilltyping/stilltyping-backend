import sys
from unittest.mock import AsyncMock

import pytest

from super_admin import cli


def test_cli_prompts_without_logging_password(monkeypatch, capsys):
    password = "A long private CLI test password"
    monkeypatch.setattr(sys, "argv", ["context-agent-super-admin", "create", "--username", "Admin"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: password)
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    cli.main()
    run.assert_awaited_once_with("create", "admin", password)
    captured = capsys.readouterr()
    assert "admin: create completed" in captured.out
    assert password not in captured.out + captured.err


def test_cli_mismatched_passwords_do_not_create_account(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["context-agent-super-admin", "create", "--username", "admin"])
    entries = iter(["A long private password", "A different private password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(entries))
    run = AsyncMock()
    monkeypatch.setattr(cli, "run", run)
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    run.assert_not_called()
