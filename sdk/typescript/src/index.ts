/**
 * Wolfenstein SDK — archives, Decls, images, audio and saves of
 * *Wolfenstein: The New Order* and *The New Colossus*.
 *
 * See `spec/SPEC.md` in the repository for the language-neutral contract.
 */

export const VERSION = "0.1.0";

export { Chunk, Game, looksTextual, validateChunk, RESOURCES_HEADER } from "./archive.js";
export type { Occurrence, Replacements } from "./archive.js";
export { SampleInfo, oggStreamLength, parseBsnf, streamContainerFor, ENGLISH_PREFIX } from "./audio.js";
export type { OggSource, RandomAccessReader } from "./audio.js";
export {
  Bim,
  buildBim,
  decode,
  decodeBim,
  encodeRgba8,
  expectedSize,
  parseBim,
  FORMAT_A8,
  FORMAT_BC1,
  FORMAT_BC3,
  FORMAT_NAMES,
  FORMAT_RGBA8,
} from "./bim.js";
export type { Image, Mip } from "./bim.js";
export { ChunkIndex, Entry, buildChunkIndex } from "./chunkIndex.js";
export type { ChunkIndexEntrySpec } from "./chunkIndex.js";
export { SYNC_MARKER, deflateExact, deflateSync, inflate, isSyncFlushed } from "./compression.js";
export type { ExactStream } from "./compression.js";
export { Decl, Node, formatNumber, formatPath, formatValue, isNumeric, parsePath, quote, unquote } from "./decl.js";
export type { NodeKind, PathTuple, Value } from "./decl.js";
export { DeclError, FormatError, ModError, NoFitError, WolfSdkError } from "./errors.js";
export { buildMasterIndex, parseMasterIndex } from "./masterIndex.js";
export type { MasterPair } from "./masterIndex.js";
export { AssetChange, Mod, applyMods, sortMods } from "./mod.js";
export type { ApplyResult, Conflict, ConflictKind, GameId, ModErrorEntry, ReadAsset, ReadFile } from "./mod.js";
export { decodePng, encodePng } from "./png.js";
export { fixSave, saveChecksum, verifySave } from "./save.js";
