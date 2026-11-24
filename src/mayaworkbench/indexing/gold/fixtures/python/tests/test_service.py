from service import count_tokens

def test_count_tokens():
    assert count_tokens("a b") == 2
