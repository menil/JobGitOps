from jobgitops.placeholder import add


def test_placeholder() -> None:
    assert add(1, 2) == 3
