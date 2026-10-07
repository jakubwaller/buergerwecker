package de.buergerwecker.app;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.Key;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

/**
 * The device credential ({"id": …, "secret": …}, the JSON text
 * client/www/store.js hands over), encrypted, out of every backup.
 *
 * The key is an AES-256-GCM key generated inside the AndroidKeyStore, and it
 * never leaves it: not into a backup, not onto another phone. What is stored is
 * "v1:" + IV + ":" + ciphertext (base64) in the private preference file
 * {@link #PREFS}, which res/xml/data_extraction_rules.xml (Android 12+, cloud
 * backup and device transfer) and res/xml/backup_rules.xml (older) exclude as
 * well, so even the ciphertext stays here. Capacitor's Preferences, which used
 * to hold the credential, are an ordinary preference file.
 *
 * No user authentication and no "unlocked device" requirement on the key: the
 * widget (EarliestSlotWidget) reads the credential in the background, with the
 * screen locked. The iOS twin is App/SecureStorePlugin.swift (the Keychain).
 */
final class SecureStore {
    static final String PREFS = "buergerwecker_secure";
    static final String KEY_CREDENTIAL = "device_credential";
    private static final String KEY_ALIAS = "buergerwecker_device_credential";
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final String TRANSFORMATION = "AES/GCM/NoPadding";
    private static final int TAG_BITS = 128;
    private static final String FORMAT = "v1";

    private SecureStore() {}

    /** Thrown when a stored value exists but cannot be read back now. */
    static final class UnreadableException extends Exception {
        UnreadableException(Throwable cause) { super(cause); }
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** The stored JSON text, or null when there is none. */
    static synchronized String read(Context context) throws UnreadableException {
        String stored = prefs(context).getString(KEY_CREDENTIAL, null);
        if (stored == null) return null;
        try {
            String[] parts = stored.split(":", -1);
            if (parts.length != 3 || !FORMAT.equals(parts[0])) throw new IllegalStateException("unknown format");
            SecretKey key = existingKey();
            if (key == null) throw new IllegalStateException("no key");
            Cipher cipher = Cipher.getInstance(TRANSFORMATION);
            cipher.init(Cipher.DECRYPT_MODE, key, new GCMParameterSpec(TAG_BITS, Base64.decode(parts[1], Base64.NO_WRAP)));
            return new String(cipher.doFinal(Base64.decode(parts[2], Base64.NO_WRAP)), StandardCharsets.UTF_8);
        } catch (Exception e) {
            // Left in place: a new credential overwrites it, and a passing
            // Keystore hiccup must not cost the device its registration.
            throw new UnreadableException(e);
        }
    }

    static synchronized void write(Context context, String value) throws Exception {
        Cipher cipher = Cipher.getInstance(TRANSFORMATION);
        cipher.init(Cipher.ENCRYPT_MODE, key()); // a fresh random IV every time
        byte[] sealed = cipher.doFinal(value.getBytes(StandardCharsets.UTF_8));
        String stored = FORMAT + ":" + Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
            + ":" + Base64.encodeToString(sealed, Base64.NO_WRAP);
        if (!prefs(context).edit().putString(KEY_CREDENTIAL, stored).commit()) {
            throw new IllegalStateException("could not save the credential");
        }
    }

    static synchronized void clear(Context context) {
        prefs(context).edit().remove(KEY_CREDENTIAL).commit();
    }

    /** What {@link #bearer} found. */
    static final class Bearer {
        static final Bearer ABSENT = new Bearer(null, false);
        static final Bearer UNREADABLE = new Bearer(null, true);
        final String header;      // "Bearer <id>.<secret>", or null
        final boolean unreadable; // stored, but the Keystore would not say now

        private Bearer(String header, boolean unreadable) {
            this.header = header;
            this.unreadable = unreadable;
        }
    }

    /** The widget's Authorization header. */
    static Bearer bearer(Context context) {
        String raw;
        try {
            raw = read(context);
        } catch (UnreadableException e) {
            return Bearer.UNREADABLE;
        }
        if (raw == null) return Bearer.ABSENT;
        try {
            JSONObject o = new JSONObject(raw);
            Object id = o.opt("id");
            String secret = o.optString("secret", "");
            String idText;
            if (id instanceof Number) idText = String.valueOf(((Number) id).longValue());
            else if (id instanceof String) idText = (String) id;
            else return Bearer.ABSENT;
            if (idText.isEmpty() || secret.isEmpty()) return Bearer.ABSENT;
            return new Bearer("Bearer " + idText + "." + secret, false);
        } catch (Exception e) {
            return Bearer.ABSENT;
        }
    }

    private static SecretKey existingKey() throws Exception {
        KeyStore store = KeyStore.getInstance(KEYSTORE);
        store.load(null);
        Key key = store.getKey(KEY_ALIAS, null);
        return key instanceof SecretKey ? (SecretKey) key : null;
    }

    private static SecretKey key() throws Exception {
        SecretKey existing = existingKey();
        if (existing != null) return existing;
        KeyGenerator generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE);
        generator.init(new KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build());
        return generator.generateKey();
    }
}
