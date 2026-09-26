"""Wolfenstein SDK — archives, Decls, images, audio and saves of
*Wolfenstein: The New Order* and *The New Colossus*.

See ``spec/SPEC.md`` in the repository for the language-neutral contract.
"""

__version__ = "0.1.0"

from .archive import Chunk, Game, Occurrence, looks_textual, validate_chunk
from .audio import SampleInfo, ogg_stream_length, parse_bsnf, stream_container_for
from .bim import Bim, Mip, build_bim, decode, encode_rgba8, parse_bim
from .chunkindex import ChunkIndex, Entry, build_chunk_index
from .compression import deflate_exact, deflate_sync, inflate, is_sync_flushed
from .decl import Decl, Node, format_path, format_value, parse_path
from .errors import DeclError, FormatError, ModError, NoFitError, WolfSdkError
from .masterindex import build_master_index, parse_master_index
from .mod import ApplyResult, AssetChange, Conflict, Mod, apply_mods
from .png import decode_png, encode_png
from .save import fix_save, save_checksum, verify_save

__all__ = [
    "ApplyResult", "AssetChange", "Bim", "Chunk", "ChunkIndex", "Conflict", "Decl",
    "DeclError", "Entry", "FormatError", "Game", "Mip", "Mod", "ModError", "NoFitError",
    "Node", "Occurrence", "SampleInfo", "WolfSdkError", "__version__", "apply_mods",
    "build_bim", "build_chunk_index", "build_master_index", "decode", "decode_png",
    "deflate_exact", "deflate_sync", "encode_png", "encode_rgba8", "fix_save",
    "format_path", "format_value", "inflate", "is_sync_flushed", "looks_textual",
    "ogg_stream_length", "parse_bim", "parse_bsnf", "parse_master_index", "parse_path",
    "save_checksum", "stream_container_for", "validate_chunk", "verify_save",
]
