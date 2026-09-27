package wolfsdk_test

import (
	"errors"
	"fmt"
	"path/filepath"

	wolfsdk "github.com/LopeKinz/wolfenstein-modloader/sdk/go"
)

func ExampleParseDecl() {
	d, err := wolfsdk.ParseDecl("{\n\tedit = {\n\t\tclipSize = 20;\n\t\tscale = 1.000000;\n\t}\n}\n")
	if err != nil {
		panic(err)
	}
	_ = d.Set("edit.clipSize", 40)
	_ = d.Set("edit.scale", 1.25)
	fmt.Print(d.Text())
	for _, p := range d.Paths() {
		fmt.Println(p.Path, p.Kind, p.Raw)
	}
	// Output:
	// {
	// 	edit = {
	// 		clipSize = 40;
	// 		scale = 1.250000;
	// 	}
	// }
	// edit.clipSize word 40
	// edit.scale word 1.250000
}

func ExampleDecl_Set_unknownPath() {
	d, _ := wolfsdk.ParseDecl("{ a = 1; }")
	err := d.Set("b", 2)
	fmt.Println(errors.Is(err, wolfsdk.ErrDecl))
	// Output: true
}

func ExampleFormatPath() {
	segs, _ := wolfsdk.ParsePath(`white."models/a.b".x`)
	fmt.Printf("%q\n", segs)
	fmt.Println(wolfsdk.FormatPath(segs))
	// Output:
	// ["white" "models/a.b" "x"]
	// white."models/a.b".x
}

func ExampleApplyMods() {
	low, _ := wolfsdk.ParseMod([]byte(`{"name": "low", "priority": 1,
		"assets": {"weapon:shotgun": {"set": {"edit.clipSize": 30}}}}`), "", "")
	high, _ := wolfsdk.ParseMod([]byte(`{"name": "high", "priority": 5,
		"assets": {"weapon:shotgun": {"set": {"edit.clipSize": 50}}}}`), "", "")
	base := map[string]string{"weapon:shotgun": "{ edit = { clipSize = 20; } }"}

	res := wolfsdk.ApplyMods([]*wolfsdk.Mod{high, low}, func(typ, name string) (string, bool) {
		s, ok := base[typ+":"+name]
		return s, ok
	}, nil)
	for _, r := range res.Results {
		fmt.Println(r.Key, "=>", r.Text)
	}
	for _, c := range res.Conflicts {
		fmt.Println(c.Kind, *c.Path, c.Mods, "winner:", c.Winner)
	}
	// Output:
	// weapon:shotgun => { edit = { clipSize = 50; } }
	// path edit.clipSize [low high] winner: high
}

func ExampleOpenGame() {
	g, err := wolfsdk.OpenGame(filepath.Join("..", "..", "spec", "vectors", "game"))
	if err != nil {
		panic(err)
	}
	for _, o := range g.Find("weapon", "weapon/shotgun_base") {
		fmt.Println(o.Chunk.IndexName, o.Entry.Offset, o.Entry.CSize, o.Entry.Compressed())
	}
	text, ok, err := g.ReadAsset("damage", "damage/tungsten/mg60")
	if err != nil || !ok {
		panic(err)
	}
	d, _ := wolfsdk.ParseDecl(string(text))
	v, _ := d.Get("edit.damageParms.maxDamage")
	fmt.Println(v)
	// Output:
	// chunk0.index 16 223 true
	// chunk1.index 96 223 true
	// 17.500000
}

func ExampleDeflateExact() {
	data := []byte("{ edit = { clipSize = 40; } }")
	slot := len(wolfsdk.DeflateSync(data)) + 20 // a slot with 20 bytes to spare
	stream, payload, ok := wolfsdk.DeflateExact(data, slot, true)
	fmt.Println(ok, len(stream) == slot, wolfsdk.IsSyncFlushed(stream))
	fmt.Printf("%q\n", payload[len(data):])
	// Output:
	// true true true
	// "\n//       "
}

func ExampleEncodePNG() {
	// A 2x1 RGBA8 BIM converted to PNG and back.
	var header [13]uint32
	header[4], header[6] = 1, wolfsdk.FormatRGBA8
	bim := wolfsdk.BuildBim(7, header, []wolfsdk.Mip{{Width: 2, Height: 1, Data: []byte{255, 0, 0, 255, 0, 0, 255, 128}}})
	b, _ := wolfsdk.ParseBim(bim)
	w, h, rgba, _ := wolfsdk.DecodeBim(b, 0)
	png, _ := wolfsdk.EncodePNG(w, h, rgba)
	w2, h2, rgba2, _ := wolfsdk.DecodePNG(png)
	fmt.Println(b.FormatName(), w2, h2, rgba2)
	// Output: RGBA8 2 1 [255 0 0 255 0 0 255 128]
}

func ExampleFixSave() {
	file := append([]byte{0, 0, 0, 0}, "payload"...)
	fmt.Println(wolfsdk.VerifySave(file))
	fixed, _ := wolfsdk.FixSave(file)
	fmt.Println(wolfsdk.VerifySave(fixed))
	fmt.Printf("%x\n", wolfsdk.SaveChecksum(nil))
	// Output:
	// false
	// true
	// 3b75655e
}
