"""log_proc 단위 테스트 — subprocess stdout/stderr를 CloudWatch로 흘리는 헬퍼.
실행: cd infra/scan-tools/shared && python -m pytest test_log_proc.py -v
"""
from normalize import log_proc


class _Proc:
    def __init__(self, stdout=b"", stderr=b""):
        self.stdout = stdout
        self.stderr = stderr


def test_prints_stdout_and_stderr_with_prefix(capsys):
    log_proc("gitleaks", _Proc(stdout=b"scanned 3 files\n", stderr=b"leak found: x\n"))
    err = capsys.readouterr().err  # log_proc는 stderr로 출력
    assert "[gitleaks:out] scanned 3 files" in err
    assert "[gitleaks:err] leak found: x" in err


def test_prepends_scan_id_tag_to_every_line(capsys):
    # scan_id를 주면 모든 상세 라인 앞에 [scan_id]가 붙어 CloudWatch quoted 필터에 걸려요.
    log_proc("semgrep", _Proc(stderr=b"Scanning 4 files\nRan 125 rules\n"),
             scan_id="xyAoth2KaYZV-20260721131439")
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert lines == [
        "[xyAoth2KaYZV-20260721131439] [semgrep:err] Scanning 4 files",
        "[xyAoth2KaYZV-20260721131439] [semgrep:err] Ran 125 rules",
    ]


def test_no_scan_id_omits_tag(capsys):
    # scan_id 없으면 tool 프리픽스만(하위호환).
    log_proc("trivy", _Proc(stdout=b"vuln scan\n"))
    assert capsys.readouterr().err.strip() == "[trivy:out] vuln scan"


def test_skips_blank_lines(capsys):
    log_proc("trivy", _Proc(stdout=b"line1\n\n  \nline2\n"))
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert lines == ["[trivy:out] line1", "[trivy:out] line2"]


def test_empty_output_prints_nothing(capsys):
    log_proc("semgrep", _Proc(stdout=b"", stderr=b""))
    assert capsys.readouterr().err == ""


def test_handles_none_streams(capsys):
    # stdout/stderr가 None이어도 안전(getattr 폴백).
    class _Bare:
        pass
    log_proc("x", _Bare())
    assert capsys.readouterr().err == ""


def test_decodes_invalid_utf8(capsys):
    log_proc("gitleaks", _Proc(stderr=b"\xff\xfe bad bytes\n"))
    err = capsys.readouterr().err
    assert "[gitleaks:err]" in err  # replace로 깨진 바이트 흡수, 크래시 없음
