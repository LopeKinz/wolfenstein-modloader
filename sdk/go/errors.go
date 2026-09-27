package wolfsdk

import "errors"

// Error kinds shared by all bindings (SPEC §1). Functions return errors that
// wrap one of these sentinels; test for them with errors.Is.
var (
	// ErrFormat reports bad magic, truncated data or implausible structure.
	ErrFormat = errors.New("wolfsdk: format error")
	// ErrDecl reports a Decl that cannot be parsed, or an unknown or malformed path.
	ErrDecl = errors.New("wolfsdk: decl error")
	// ErrNoFit reports a replacement that does not fit into its archive slot;
	// rebuild the chunk instead.
	ErrNoFit = errors.New("wolfsdk: does not fit")
	// ErrMod reports an invalid mod.json file.
	ErrMod = errors.New("wolfsdk: mod error")
	// ErrNotFound reports that an asset does not exist in the game (Go
	// counterpart of the Python binding's KeyError in Game.write_asset).
	ErrNotFound = errors.New("wolfsdk: not found")
)
