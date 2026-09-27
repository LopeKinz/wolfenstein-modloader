/** Error kinds shared by all bindings (SPEC §1). */

/** Base class of every error raised by the SDK. */
export class WolfSdkError extends Error {
  constructor(message?: string) {
    super(message);
    this.name = new.target.name;
  }
}

/** Bad magic, truncated data or implausible structure. */
export class FormatError extends WolfSdkError {}

/** A Decl cannot be parsed, or a path is unknown or malformed. */
export class DeclError extends WolfSdkError {}

/** A replacement does not fit into its archive slot; rebuild instead. */
export class NoFitError extends WolfSdkError {}

/** A mod.json file is invalid. */
export class ModError extends WolfSdkError {}
