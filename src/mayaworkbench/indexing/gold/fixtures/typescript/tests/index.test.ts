import { tokenize } from "../src/index";
test("tokenize", () => expect(tokenize("a b")).toEqual(["a", "b"]));
