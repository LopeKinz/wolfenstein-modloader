package wolfsdk

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// SetPair is one "path": value entry of a mod's set object. Value is a
// string, float64 or bool.
type SetPair struct {
	Path  string
	Value any
}

// AssetChange is one asset entry of a mod (SPEC §7).
type AssetChange struct {
	Key     string    // "type:name"
	Set     []SetPair // in file order
	File    string    // relative path of a replacement file, if HasFile
	HasFile bool
}

// Type returns the part of Key before the first ':'.
func (a AssetChange) Type() string { t, _, _ := strings.Cut(a.Key, ":"); return t }

// Name returns the part of Key after the first ':'.
func (a AssetChange) Name() string { _, n, _ := strings.Cut(a.Key, ":"); return n }

// Mod is a parsed mod.json.
type Mod struct {
	Name        string
	Priority    int
	Game        string // "tno", "tnc" or "" (unspecified)
	Version     string
	Description string
	Assets      []AssetChange
	Root        string // directory the mod was loaded from ("" if none)
}

// ReadFile reads a file relative to the mod directory.
func (m *Mod) ReadFile(rel string) (string, error) {
	if m.Root == "" {
		return "", fmt.Errorf("%w: %s: cannot resolve file %q without a mod directory", ErrMod, m.Name, rel)
	}
	b, err := os.ReadFile(filepath.Join(m.Root, rel))
	if err != nil {
		return "", err
	}
	return string(b), nil
}

// ------------------------------------------------ order-preserving JSON

type jsonObject struct {
	keys []string
	vals map[string]any
}

func (o *jsonObject) get(k string) (any, bool) { v, ok := o.vals[k]; return v, ok }

func decodeJSONValue(dec *json.Decoder) (any, error) {
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	switch t := tok.(type) {
	case json.Delim:
		switch t {
		case '{':
			obj := &jsonObject{vals: map[string]any{}}
			for dec.More() {
				kt, err := dec.Token()
				if err != nil {
					return nil, err
				}
				k, ok := kt.(string)
				if !ok {
					return nil, errors.New("object key is not a string")
				}
				v, err := decodeJSONValue(dec)
				if err != nil {
					return nil, err
				}
				if _, dup := obj.vals[k]; !dup {
					obj.keys = append(obj.keys, k)
				}
				obj.vals[k] = v
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			return obj, nil
		case '[':
			var arr []any
			for dec.More() {
				v, err := decodeJSONValue(dec)
				if err != nil {
					return nil, err
				}
				arr = append(arr, v)
			}
			if _, err := dec.Token(); err != nil {
				return nil, err
			}
			if arr == nil {
				arr = []any{}
			}
			return arr, nil
		}
		return nil, fmt.Errorf("unexpected %v", t)
	case json.Number:
		f, err := strconv.ParseFloat(string(t), 64)
		if err != nil && !errors.Is(err, strconv.ErrRange) {
			return nil, err
		}
		return f, nil
	default: // string, bool, nil
		return tok, nil
	}
}

func decodeOrderedJSON(data []byte) (any, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	v, err := decodeJSONValue(dec)
	if err != nil {
		return nil, err
	}
	if _, err := dec.Token(); err != io.EOF {
		return nil, errors.New("extra data after JSON value")
	}
	return v, nil
}

// ------------------------------------------------------------- loading

// ParseMod parses mod.json content. defaultName is used when the file has no
// name ("" means "mod"); root is the mod directory for "file" references.
func ParseMod(data []byte, defaultName, root string) (*Mod, error) {
	if defaultName == "" {
		defaultName = "mod"
	}
	v, err := decodeOrderedJSON(data)
	if err != nil {
		return nil, fmt.Errorf("%w: invalid JSON: %v", ErrMod, err)
	}
	obj, ok := v.(*jsonObject)
	if !ok {
		return nil, fmt.Errorf("%w: mod.json must contain a JSON object", ErrMod)
	}
	m := &Mod{Root: root}
	if p, ok := obj.get("priority"); ok {
		f, isNum := p.(float64)
		if !isNum || f != math.Trunc(f) || math.IsInf(f, 0) {
			return nil, fmt.Errorf("%w: priority must be an integer", ErrMod)
		}
		m.Priority = int(f)
	}
	game, ok := obj.get("game")
	if !ok {
		game = obj.vals["spiel"]
	}
	if game != nil {
		g, _ := game.(string)
		if g != "tno" && g != "tnc" {
			return nil, fmt.Errorf("%w: unknown game %v (expected 'tno' or 'tnc')", ErrMod, game)
		}
		m.Game = g
	}
	var assets *jsonObject
	if a, ok := obj.get("assets"); ok {
		if assets, ok = a.(*jsonObject); !ok {
			return nil, fmt.Errorf("%w: assets must be an object", ErrMod)
		}
	} else {
		assets = &jsonObject{vals: map[string]any{}}
		for _, k := range obj.keys {
			if strings.Contains(k, ":") {
				assets.keys = append(assets.keys, k)
				assets.vals[k] = obj.vals[k]
			}
		}
	}
	for _, key := range assets.keys {
		if !strings.Contains(key, ":") || strings.HasPrefix(key, ":") || strings.HasSuffix(key, ":") {
			return nil, fmt.Errorf("%w: asset key %q must be 'type:name'", ErrMod, key)
		}
		change, ok := assets.vals[key].(*jsonObject)
		if !ok {
			return nil, fmt.Errorf("%w: %s: change must be an object", ErrMod, key)
		}
		sets := change.vals["set"]
		file := change.vals["file"]
		if sets == nil && file == nil {
			return nil, fmt.Errorf("%w: %s: needs 'set' or 'file'", ErrMod, key)
		}
		ac := AssetChange{Key: key}
		if sets != nil {
			so, ok := sets.(*jsonObject)
			if !ok {
				return nil, fmt.Errorf("%w: %s: 'set' must be an object", ErrMod, key)
			}
			for _, p := range so.keys {
				switch val := so.vals[p].(type) {
				case string, float64, bool:
					ac.Set = append(ac.Set, SetPair{p, val})
				default:
					return nil, fmt.Errorf("%w: %s: value of %q must be string, number or bool", ErrMod, key, p)
				}
			}
		}
		if file != nil {
			fs, ok := file.(string)
			if !ok {
				return nil, fmt.Errorf("%w: %s: 'file' must be a string", ErrMod, key)
			}
			ac.File, ac.HasFile = fs, true
		}
		m.Assets = append(m.Assets, ac)
	}
	if n, ok := obj.vals["name"].(string); ok && n != "" {
		m.Name = n
	} else {
		m.Name = defaultName
	}
	m.Version, _ = obj.vals["version"].(string)
	m.Description, _ = obj.vals["description"].(string)
	return m, nil
}

// LoadMod loads mod.json, or a directory containing one. The mod's default
// name is the directory name.
func LoadMod(dirOrFile string) (*Mod, error) {
	path := dirOrFile
	if st, err := os.Stat(path); err == nil && st.IsDir() {
		path = filepath.Join(path, "mod.json")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	root := filepath.Dir(abs)
	data, err := os.ReadFile(abs)
	if err != nil {
		return nil, err
	}
	return ParseMod(data, filepath.Base(root), root)
}

// ------------------------------------------------------------ layering

// Conflict is a non-blocking conflict between mods (SPEC §7.1).
type Conflict struct {
	Kind   string   `json:"kind"` // "path", "file" or "file-over-set"
	Asset  string   `json:"asset"`
	Path   *string  `json:"path"` // nil unless Kind == "path"
	Mods   []string `json:"mods"`
	Winner string   `json:"winner"`
}

// ApplyError is a per-asset error recorded by ApplyMods.
type ApplyError struct {
	Asset   string `json:"asset"`
	Mod     string `json:"mod"`
	Message string `json:"message"`
}

// AssetResult is the new text of one asset.
type AssetResult struct {
	Key  string
	Text string
}

// ApplyResult is the outcome of ApplyMods. Results are ordered by first
// appearance of the asset across the priority-sorted mods.
type ApplyResult struct {
	Results   []AssetResult
	Conflicts []Conflict
	Errors    []ApplyError
}

// Result returns the new text of key.
func (r *ApplyResult) Result(key string) (string, bool) {
	for _, a := range r.Results {
		if a.Key == key {
			return a.Text, true
		}
	}
	return "", false
}

// SortMods returns mods sorted by priority ascending; ties keep input order.
func SortMods(mods []*Mod) []*Mod {
	out := append([]*Mod{}, mods...)
	sort.SliceStable(out, func(i, j int) bool { return out[i].Priority < out[j].Priority })
	return out
}

type modChange struct {
	mod *Mod
	ch  *AssetChange
}

// ApplyMods layers mods over the game's assets in memory (SPEC §7.1).
// readAsset returns the current text of type:name (ok=false if missing);
// readFile reads a mod-relative file (nil = (*Mod).ReadFile).
func ApplyMods(mods []*Mod, readAsset func(typ, name string) (string, bool), readFile func(m *Mod, rel string) (string, error)) *ApplyResult {
	if readFile == nil {
		readFile = func(m *Mod, rel string) (string, error) { return m.ReadFile(rel) }
	}
	perAsset := map[string][]modChange{}
	var order []string
	for _, m := range SortMods(mods) {
		for i := range m.Assets {
			ch := &m.Assets[i]
			if _, ok := perAsset[ch.Key]; !ok {
				order = append(order, ch.Key)
			}
			perAsset[ch.Key] = append(perAsset[ch.Key], modChange{m, ch})
		}
	}
	res := &ApplyResult{Results: []AssetResult{}, Conflicts: []Conflict{}, Errors: []ApplyError{}}
	for _, key := range order {
		changes := perAsset[key]
		// conflicts
		var fileMods []string
		for _, c := range changes {
			if c.ch.HasFile {
				fileMods = append(fileMods, c.mod.Name)
			}
		}
		if len(fileMods) > 1 {
			res.Conflicts = append(res.Conflicts, Conflict{"file", key, nil, fileMods, fileMods[len(fileMods)-1]})
		}
		pathMods := map[string][]string{}
		var pathOrder []string
		for _, c := range changes {
			for _, sp := range c.ch.Set {
				lst, ok := pathMods[sp.Path]
				if !ok {
					pathOrder = append(pathOrder, sp.Path)
				}
				if !containsString(lst, c.mod.Name) {
					lst = append(lst, c.mod.Name)
				}
				pathMods[sp.Path] = lst
			}
		}
		for _, p := range pathOrder {
			if names := pathMods[p]; len(names) > 1 {
				path := p
				res.Conflicts = append(res.Conflicts, Conflict{"path", key, &path, names, names[len(names)-1]})
			}
		}
		for i, c := range changes {
			if !c.ch.HasFile {
				continue
			}
			var setters []string
			for _, pc := range changes[:i] {
				if len(pc.ch.Set) > 0 && pc.mod != c.mod {
					setters = append(setters, pc.mod.Name)
				}
			}
			if len(setters) > 0 {
				res.Conflicts = append(res.Conflicts, Conflict{"file-over-set", key, nil, append(setters, c.mod.Name), c.mod.Name})
			}
		}

		typ, name, _ := strings.Cut(key, ":")
		text, ok := readAsset(typ, name)
		if !ok {
			res.Errors = append(res.Errors, ApplyError{key, changes[0].mod.Name, "asset not found"})
			continue
		}
		for _, c := range changes {
			if c.ch.HasFile {
				t, err := readFile(c.mod, c.ch.File)
				if err != nil {
					res.Errors = append(res.Errors, ApplyError{key, c.mod.Name, fmt.Sprintf("cannot read %s: %v", c.ch.File, err)})
					continue
				}
				text = t
			}
			if len(c.ch.Set) == 0 {
				continue
			}
			decl, err := ParseDecl(text)
			if err != nil {
				res.Errors = append(res.Errors, ApplyError{key, c.mod.Name, err.Error()})
				continue
			}
			for _, sp := range c.ch.Set {
				if err := decl.Set(sp.Path, sp.Value); err != nil {
					res.Errors = append(res.Errors, ApplyError{key, c.mod.Name, err.Error()})
				}
			}
			text = decl.Text()
		}
		res.Results = append(res.Results, AssetResult{key, text})
	}
	return res
}

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
