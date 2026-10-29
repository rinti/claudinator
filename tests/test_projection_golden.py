import hashlib
import json
import re
from pathlib import Path

import claudinator.archive as archive_module
from claudinator.archive import Archive
from claudinator.layout import Config
from claudinator.record import PROJECTION_VERSION
from helpers import projection_dump

CORPUS = Path(__file__).parent / "fixtures" / "corpus"

GOLDEN = {
    5: "6f14f11974a85ce9aea3ecccadaabc51287a00690227347cce75b19bf3743cb2",
}

GOLDEN_DDL = {
    5: "a03aed599c71ce8cccce523155aee0c76c7eff32f40a8e9049a3aa4660d14c19",
}


def test_projection_of_the_fixture_corpus_matches_the_recorded_digest(tmp_path):
    cfg = Config(db=tmp_path / "golden.db", claude_dir=CORPUS / "claude", desktop_dir=CORPUS / "desktop")
    with Archive.open(cfg) as archive:
        assert archive.sweep().errors == ()
        dump = projection_dump(archive, CORPUS)
    digest = hashlib.sha256(json.dumps(dump, ensure_ascii=False).encode()).hexdigest()
    assert PROJECTION_VERSION in GOLDEN, f"record GOLDEN[{PROJECTION_VERSION}] = {digest!r}"
    assert digest == GOLDEN[PROJECTION_VERSION], (
        f"projection output changed: bump PROJECTION_VERSION and record the new digest {digest!r}"
    )


def test_the_projection_ddl_matches_the_digest_recorded_for_its_version():
    statements = " ".join(re.sub(r"--[^\n]*", "", archive_module._PROJECTION_DDL).split())
    digest = hashlib.sha256(statements.encode()).hexdigest()
    assert PROJECTION_VERSION in GOLDEN_DDL, f"record GOLDEN_DDL[{PROJECTION_VERSION}] = {digest!r}"
    assert digest == GOLDEN_DDL[PROJECTION_VERSION], (
        "the projection DDL in schema.sql changed, and open() rebuilds only when PROJECTION_VERSION grows:"
        f" bump PROJECTION_VERSION, record both digests for it, and use {digest!r} here"
    )
