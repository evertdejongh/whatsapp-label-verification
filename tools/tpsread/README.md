Vendored copy of [tpsread](https://github.com/galler-alexander/tpsread) by Alexander
Galler-Haller, used by `sync_reference_tables.py` to read Clarion TopSpeed (.TPS)
files directly. GPL-3.0 licensed (see LICENSE) -- not republished anywhere, used
only as a local tool to pull data into this project's own AWS account.

Not available as an installable package (no setup.py/pyproject.toml upstream),
hence vendoring rather than a pip/requirements.txt entry. Requires:

    pip install six "construct==2.5.3"

(the newer construct 2.9+ API removed the names this library's parser uses, e.g.
UBInt32/ULInt8/the adapters module -- 2.5.3 is the last release with the old API).
