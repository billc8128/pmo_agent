package claudecode

import (
	"encoding/json"
)

// Tiny helpers shared by parser_test.go.
//
// They live in their own *_test.go file so we can freely add internal
// test utilities without touching production files.

func jsonline(m map[string]any) string {
	b, err := json.Marshal(m)
	if err != nil {
		panic(err)
	}
	return string(b) + "\n"
}

func jsonUnmarshal(s string, out any) error {
	return json.Unmarshal([]byte(s), out)
}
