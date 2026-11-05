"""Temporary CI diagnostics: expose failing test names as check annotations."""


def pytest_terminal_summary(terminalreporter):
    for outcome in ("failed", "error"):
        for report in terminalreporter.stats.get(outcome, []):
            name = report.nodeid.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            terminalreporter.write_line("::error title=pytest failure::" + name)
