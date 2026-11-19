"""The curl fallback only runs when the weather section is already in trouble.

urllib intermittently times out in the TLS handshake on the Pi while Open-Meteo
itself is healthy, so `fetch_json` retries through curl's separate TLS stack
before giving up.  That path is invisible in normal operation: it fires only
after a failure, and a mistake in it costs the briefing its whole weather
section.  These tests pin the three outcomes (urllib wins, curl rescues, both
fail) and make sure a double failure still names both causes instead of
surfacing an opaque traceback.
"""

import io
import json
import subprocess

import pytest

import weather_snapshot


PAYLOAD = {'daily': {'temperature_2m_max': [79, 78]}, 'hourly': {'time': []}}


@pytest.fixture
def no_curl(monkeypatch):
    """Fail loudly if the healthy path shells out."""
    def forbidden(*args, **kwargs):
        raise AssertionError('curl must not run when urllib succeeds')

    monkeypatch.setattr(weather_snapshot.subprocess, 'run', forbidden)


def fake_urlopen(payload=None, error=None):
    """Stand in for urlopen: either a JSON body or a raised transport error."""
    def opener(req, timeout=None):
        opener.calls.append((req, timeout))
        if error is not None:
            raise error
        return io.StringIO(json.dumps(payload))

    opener.calls = []
    return opener


def fake_run(stdout=None, error=None):
    def runner(argv, **kwargs):
        runner.calls.append((argv, kwargs))
        if error is not None:
            raise error
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr='')

    runner.calls = []
    return runner


def test_urllib_success_returns_payload(monkeypatch, no_curl):
    opener = fake_urlopen(payload=PAYLOAD)
    monkeypatch.setattr(weather_snapshot.urllib.request, 'urlopen', opener)

    assert weather_snapshot.fetch_json('https://example/forecast') == PAYLOAD
    assert len(opener.calls) == 1


def test_urllib_request_is_identified_and_bounded(monkeypatch, no_curl):
    opener = fake_urlopen(payload=PAYLOAD)
    monkeypatch.setattr(weather_snapshot.urllib.request, 'urlopen', opener)

    weather_snapshot.fetch_json('https://example/forecast')
    req, timeout = opener.calls[0]

    # Identify the client honestly rather than spoofing a browser, and never
    # block the briefing's `wait` without a bound.
    assert req.get_header('User-agent') == 'OpenClaw-Briefing/1.0'
    assert timeout == weather_snapshot.URLLIB_TIMEOUT
    assert timeout is not None


def test_curl_rescues_a_tls_handshake_timeout(monkeypatch):
    """The whole point: one failed handshake must not drop the section."""
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=TimeoutError('_ssl.c:1112: handshake timed out')),
    )
    runner = fake_run(stdout=json.dumps(PAYLOAD))
    monkeypatch.setattr(weather_snapshot.subprocess, 'run', runner)

    assert weather_snapshot.fetch_json('https://example/forecast') == PAYLOAD
    assert len(runner.calls) == 1


def test_curl_invocation_fails_closed_and_stays_bounded(monkeypatch):
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=OSError('unreachable')),
    )
    runner = fake_run(stdout=json.dumps(PAYLOAD))
    monkeypatch.setattr(weather_snapshot.subprocess, 'run', runner)

    weather_snapshot.fetch_json('https://example/forecast')
    argv, kwargs = runner.calls[0]

    assert argv[0] == 'curl'
    assert argv[-1] == 'https://example/forecast'
    # --fail: an HTTP error page is not a forecast.
    assert '--fail' in argv
    # Three bounds, so the retry loop cannot outlive the briefing.
    assert '--connect-timeout' in argv and '--max-time' in argv
    assert '--retry-max-time' in argv
    assert kwargs['timeout'] == weather_snapshot.CURL_TIMEOUT
    assert kwargs['check'] is True
    # Belt and braces: the process bound must cover the retry budget.
    retry_max = int(argv[argv.index('--retry-max-time') + 1])
    assert weather_snapshot.CURL_TIMEOUT >= retry_max


def test_both_paths_failing_names_both_causes(monkeypatch):
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=OSError('handshake timed out')),
    )
    monkeypatch.setattr(
        weather_snapshot.subprocess, 'run',
        fake_run(error=subprocess.CalledProcessError(
            7, 'curl', stderr='curl: (7) Failed to connect\n')),
    )

    with pytest.raises(RuntimeError) as excinfo:
        weather_snapshot.fetch_json('https://example/forecast')

    message = str(excinfo.value)
    # main() prints this verbatim as "Forecast: unavailable (...)", so both
    # halves have to be diagnosable from the briefing alone.
    assert 'handshake timed out' in message
    assert 'Failed to connect' in message


def test_silent_curl_failure_still_reports_its_exit_code(monkeypatch):
    """A curl that dies without stderr must not produce an empty reason."""
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=OSError('handshake timed out')),
    )
    monkeypatch.setattr(
        weather_snapshot.subprocess, 'run',
        fake_run(error=subprocess.CalledProcessError(28, 'curl', stderr='')),
    )

    with pytest.raises(RuntimeError, match='exit 28'):
        weather_snapshot.fetch_json('https://example/forecast')


def test_curl_timeout_is_reported_not_raised_raw(monkeypatch):
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=OSError('handshake timed out')),
    )
    monkeypatch.setattr(
        weather_snapshot.subprocess, 'run',
        fake_run(error=subprocess.TimeoutExpired('curl', weather_snapshot.CURL_TIMEOUT)),
    )

    # TimeoutExpired is not a CalledProcessError; it must still land as a
    # RuntimeError the caller's except clause can print.
    with pytest.raises(RuntimeError, match='timed out'):
        weather_snapshot.fetch_json('https://example/forecast')


def test_curl_returning_garbage_is_a_fetch_failure(monkeypatch):
    """A 200 that isn't JSON is a failed fetch, not a crash."""
    monkeypatch.setattr(
        weather_snapshot.urllib.request, 'urlopen',
        fake_urlopen(error=OSError('handshake timed out')),
    )
    monkeypatch.setattr(
        weather_snapshot.subprocess, 'run',
        fake_run(stdout='<html>502 Bad Gateway</html>'),
    )

    with pytest.raises(RuntimeError) as excinfo:
        weather_snapshot.fetch_json('https://example/forecast')
    assert not isinstance(excinfo.value, json.JSONDecodeError)


def test_missing_coordinates_report_unavailable(monkeypatch, capsys):
    """No coords is a configuration gap, not a fetch: never hit the network."""
    monkeypatch.setattr(weather_snapshot.local_config, 'get',
                        lambda key, default=None: default)
    monkeypatch.setattr(weather_snapshot, 'fetch_json',
                        lambda url: pytest.fail('must not fetch without coords'))

    assert weather_snapshot.main() == 0
    assert 'Forecast: unavailable' in capsys.readouterr().out


def test_fetch_failure_degrades_to_one_line(monkeypatch, capsys):
    """briefing_parent keys off a non-empty first line, so main must not raise."""
    monkeypatch.setattr(weather_snapshot.local_config, 'get',
                        lambda key, default=None: '21.3' if key != 'HOME_ELEV_M' else '5')
    monkeypatch.setattr(weather_snapshot, 'fetch_json',
                        lambda url: (_ for _ in ()).throw(RuntimeError('both paths failed')))

    assert weather_snapshot.main() == 0
    out = capsys.readouterr().out
    assert out.startswith('Forecast: unavailable (both paths failed)')
