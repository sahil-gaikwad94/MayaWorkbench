package main
import "testing"
func TestTokenize(t *testing.T) { if len(Tokenize("a b")) != 2 { t.Fatal("bad") } }
