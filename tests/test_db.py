from pubmedgemini.db import is_healthy, open_pool


def test_is_healthy_true_with_a_database(pool):
    assert is_healthy(pool) is True


def test_is_healthy_false_when_database_unreachable():  # Review Focus 4
    bad = open_pool("postgresql://u:p@127.0.0.1:1/x")
    try:
        assert is_healthy(bad) is False
    finally:
        bad.close()
