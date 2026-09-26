// Package wolfsdk is the Go binding of the Wolfenstein modding SDK: archives,
// Decls, BIM images, audio descriptors, save-game checksums and mod.json
// layering for Wolfenstein: The New Order (id Tech 5) and the Decl format of
// The New Colossus.
//
// It implements the language-neutral contract in spec/SPEC.md of the
// wolfenstein-modloader repository and passes the shared test vectors in
// spec/vectors. The package uses the standard library only.
//
// # Overview
//
//   - Archives: [OpenGame] reads base/master.index and every chunk index;
//     [Game.Find], [Game.Read] and [Game.ReadAsset] read assets;
//     [Game.WriteInPlace] / [Game.WriteAsset] patch a slot without moving
//     anything (the replacement is compressed to exactly the slot size, see
//     [DeflateExact]); [Game.Rebuild] rewrites a whole .resources file when a
//     replacement does not fit ([ErrNoFit]).
//   - Decls: [ParseDecl] returns a span-preserving editor; [Decl.Set] changes
//     one value and leaves every other byte untouched.
//   - Mods: [LoadMod] / [ParseMod] read mod.json; [ApplyMods] layers mods in
//     memory and reports conflicts.
//   - Images: [ParseBim], [DecodeBim], [EncodeRGBA8], [EncodePNG], [DecodePNG].
//   - Audio: [ParseBsnf], [StreamContainerFor], [OggStreamLength].
//   - Saves: [SaveChecksum], [VerifySave], [FixSave].
//
// Errors wrap the sentinels [ErrFormat], [ErrDecl], [ErrNoFit] and [ErrMod];
// test for them with errors.Is.
//
// # Example
//
//	g, err := wolfsdk.OpenGame(`C:\Games\Wolfenstein The New Order`)
//	if err != nil {
//		log.Fatal(err)
//	}
//	text, ok, err := g.ReadAsset("weapon", "weapon/shotgun_base")
//	if err != nil || !ok {
//		log.Fatal("asset not found", err)
//	}
//	d, err := wolfsdk.ParseDecl(string(text))
//	if err != nil {
//		log.Fatal(err)
//	}
//	if err := d.Set("edit.validAmmoClips.item[0].clipSize", 40); err != nil {
//		log.Fatal(err)
//	}
//	if _, err := g.WriteAsset("weapon", "weapon/shotgun_base", []byte(d.Text())); err != nil {
//		log.Fatal(err) // errors.Is(err, wolfsdk.ErrNoFit): rebuild the chunk instead
//	}
//
// Always back up the game's base/ directory before writing to it. The full
// command-line tool with backups, journaling and revert lives in the Python
// package (sdk/python).
package wolfsdk
