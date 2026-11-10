"""infra/write_deploy_key.sh turns the VPS_SSH_KEY secret into a usable private key file.

Every key here is a throwaway made with ssh-keygen in tmp_path.
"""

import base64
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "write_deploy_key.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"
BAD_MESSAGE = ("VPS_SSH_KEY is not a readable private key "
               "(it may have been damaged when it was pasted)")


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "orig"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "throwaway",
                    "-f", str(path)], check=True)
    text = path.read_text()
    fingerprint = subprocess.run(["ssh-keygen", "-lf", str(path)], check=True,
                                 capture_output=True, text=True).stdout.strip()
    return text, fingerprint


def _run(tmp_path, secret, env_secret=True):
    target = tmp_path / "out" / "deploy_key"
    target.parent.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "VPS_SSH_KEY"}
    if env_secret:
        env["VPS_SSH_KEY"] = secret
    done = subprocess.run(["bash", str(SCRIPT), str(target)], env=env, check=False,
                          capture_output=True, text=True, timeout=60)
    return done, target


def _body_lines(text):
    return [ln for ln in text.splitlines() if ln and not ln.startswith("-----")]


def _assert_quiet(done, text, secret):
    """Neither stream may hold a line of the key body, or the secret itself."""
    out = done.stdout + done.stderr
    for line in _body_lines(text):
        assert line not in out
    assert secret.strip() not in out


def _assert_ok(done, target, text, fingerprint, secret):
    assert done.returncode == 0, done.stderr
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    lines = done.stdout.splitlines()
    assert lines == [fingerprint]
    assert re.fullmatch(r"\d+ SHA256:\S+.*", lines[0])
    assert done.stderr == ""
    _assert_quiet(done, text, secret)
    derived = subprocess.run(["ssh-keygen", "-y", "-f", str(target)], check=True,
                             capture_output=True, text=True).stdout
    assert derived.startswith("ssh-ed25519 ")


def _b64(text):
    return base64.b64encode(text.encode()).decode()


def _wrap_messily(encoded):
    pieces = [encoded[i:i + 80] for i in range(0, len(encoded), 80)]
    seps = [" \r\n", "\n", "\r\n  ", " \n\t", "\n\n"]
    out = "  "
    for i, piece in enumerate(pieces):
        out += piece + seps[i % len(seps)]
    return out + " \r\n"


def test_raw_key_as_is(tmp_path, key):
    text, fp = key
    done, target = _run(tmp_path, text)
    _assert_ok(done, target, text, fp, text)


def test_raw_key_with_crlf(tmp_path, key):
    text, fp = key
    secret = text.replace("\n", "\r\n")
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)
    assert b"\r" not in target.read_bytes()


def test_raw_key_with_trailing_spaces(tmp_path, key):
    text, fp = key
    secret = "".join(f"{ln}  \t\n" for ln in text.splitlines())
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)


def test_raw_key_without_final_newline(tmp_path, key):
    text, fp = key
    secret = text.rstrip("\n")
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)
    assert target.read_bytes().endswith(b"-----\n")
    assert not target.read_bytes().endswith(b"\n\n")


def test_raw_key_with_blank_lines_around(tmp_path, key):
    text, fp = key
    secret = "\n\n  \n" + text + "\n\n \n"
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)
    assert target.read_text() == text


def test_base64_single_line(tmp_path, key):
    text, fp = key
    secret = _b64(text)
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)


def test_base64_wrapped_with_stray_whitespace(tmp_path, key):
    text, fp = key
    secret = _wrap_messily(_b64(text))
    done, target = _run(tmp_path, secret)
    _assert_ok(done, target, text, fp, secret)


def test_damaged_key_is_refused(tmp_path, key):
    text, _ = key
    body = _body_lines(text)[1]
    cut = text.index(body) + len(body) // 2
    secret = text[:cut] + text[cut + 1:]
    done, target = _run(tmp_path, secret)
    assert done.returncode != 0
    assert done.stderr.strip() == BAD_MESSAGE
    assert done.stdout == ""
    assert not target.exists()
    _assert_quiet(done, text, secret)


def test_damaged_base64_is_refused(tmp_path, key):
    text, _ = key
    encoded = _b64(text)
    secret = encoded[:40] + encoded[41:]
    done, target = _run(tmp_path, secret)
    assert done.returncode != 0
    assert done.stderr.strip() == BAD_MESSAGE
    assert not target.exists()


def test_empty_secret_is_refused(tmp_path):
    done, target = _run(tmp_path, "")
    assert done.returncode != 0
    assert "VPS_SSH_KEY" in done.stderr
    assert done.stdout == ""
    assert not target.exists()


def test_unset_secret_is_refused(tmp_path):
    done, target = _run(tmp_path, "", env_secret=False)
    assert done.returncode != 0
    assert "VPS_SSH_KEY" in done.stderr
    assert not target.exists()


def test_workflow_uses_the_script():
    text = WORKFLOW.read_text()
    assert 'bash infra/write_deploy_key.sh "$HOME/.ssh/deploy_key"' in text
    assert not re.search(r"printf.*>\s*~/\.ssh/deploy_key", text)
    assert "StrictHostKeyChecking=yes" in text
    # the key is written (and checked) before anything is copied
    assert text.index("write_deploy_key.sh") < text.index("scp ")
