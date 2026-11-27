(() => {
  "use strict";

  function create(prefix, recoveryOwner) {
    const scopedKey = (key, owner) => `${prefix}${key}\0${owner}`;

    function read(key) {
      const owner = recoveryOwner();
      if (!owner) return null;
      try {
        const value = sessionStorage.getItem(scopedKey(key, owner));
        return value === null ? null : JSON.parse(value);
      } catch {
        return null;
      }
    }

    function write(key, value) {
      const owner = recoveryOwner();
      if (!owner) return true;
      try {
        const storageKey = scopedKey(key, owner);
        if (value === null) sessionStorage.removeItem(storageKey);
        else sessionStorage.setItem(storageKey, JSON.stringify(value));
        return true;
      } catch {
        return false;
      }
    }

    function clear(keyPrefix = "") {
      try {
        for (let i = sessionStorage.length - 1; i >= 0; i--) {
          const key = sessionStorage.key(i);
          if (key?.startsWith(prefix + keyPrefix)) sessionStorage.removeItem(key);
        }
      } catch {
        // Access to the browser's session storage can be disabled.
      }
    }

    function hasEntries() {
      const owner = recoveryOwner();
      if (!owner) return false;
      try {
        for (let i = 0; i < sessionStorage.length; i++) {
          const key = sessionStorage.key(i);
          if (key?.startsWith(prefix) && key.endsWith(`\0${owner}`)) return true;
        }
      } catch {
        // The editor can still keep its in-memory state.
      }
      return false;
    }

    return { read, write, clear, hasEntries };
  }

  window.DraftRecovery = { create };
})();
