const CANONICAL_PREFIX = 'tc_studio_'
const LEGACY_PREFIX = 'tim_'

function key(prefix: string, suffix: string): string {
  return `${prefix}${suffix}`
}

/**
 * Read application-owned browser state from the TC Studio namespace.
 *
 * Existing TIM Studio state is imported lazily into the canonical key so an
 * upgrade does not discard tabs, theme, history, or the development token.
 */
export function readTcStorage(storage: Storage, suffix: string): string | null {
  try {
    const canonicalKey = key(CANONICAL_PREFIX, suffix)
    const canonicalValue = storage.getItem(canonicalKey)
    if (canonicalValue !== null) return canonicalValue

    const legacyValue = storage.getItem(key(LEGACY_PREFIX, suffix))
    if (legacyValue !== null) {
      try {
        storage.setItem(canonicalKey, legacyValue)
      } catch {
        // Private browsing or quota restrictions must not break the app.
      }
    }
    return legacyValue
  } catch {
    return null
  }
}

export function writeTcStorage(storage: Storage, suffix: string, value: string): void {
  storage.setItem(key(CANONICAL_PREFIX, suffix), value)
}

export function removeTcStorage(storage: Storage, suffix: string): void {
  try {
    storage.removeItem(key(CANONICAL_PREFIX, suffix))
    storage.removeItem(key(LEGACY_PREFIX, suffix))
  } catch {
    // Clearing optional browser state should remain best effort.
  }
}
