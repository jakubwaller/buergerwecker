package de.buergerwecker.app;

import android.content.Context;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.google.android.gms.tasks.Task;
import com.google.android.play.core.integrity.IntegrityManagerFactory;
import com.google.android.play.core.integrity.StandardIntegrityException;
import com.google.android.play.core.integrity.StandardIntegrityManager;
import com.google.android.play.core.integrity.StandardIntegrityManager.PrepareIntegrityTokenRequest;
import com.google.android.play.core.integrity.StandardIntegrityManager.StandardIntegrityToken;
import com.google.android.play.core.integrity.StandardIntegrityManager.StandardIntegrityTokenProvider;
import com.google.android.play.core.integrity.StandardIntegrityManager.StandardIntegrityTokenRequest;
import com.google.android.play.core.integrity.model.StandardIntegrityErrorCode;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * Play Integrity verdict for a registration (server side: app/integrity.py).
 *
 * The server only creates a device row, or takes a new push token, for a token
 * from here: a script can receive FCM pushes without a phone, but cannot get
 * Google Play to vouch for the app on a real device. token({ pushToken })
 * asks for a standard-request token whose request hash is the lowercase hex
 * SHA-256 of the push token itself, which is how the server ties the verdict
 * to exactly that token. Only a build installed from Google Play gets a
 * passing verdict (client/android/PLAY.md, "Play Integrity").
 */
@CapacitorPlugin(name = "Integrity")
public class IntegrityPlugin extends Plugin {
    // Preparing is the slow, quota-counted step (it warms up the token on
    // Google's side), so it happens once and the provider is reused.
    private StandardIntegrityTokenProvider provider;

    @PluginMethod
    public void token(PluginCall call) {
        String pushToken = call.getString("pushToken");
        if (pushToken == null || pushToken.isEmpty()) {
            call.reject("pushToken is required");
            return;
        }
        long project = cloudProjectNumber();
        if (project == 0) {
            call.reject("No Google Cloud project number: this build has no google-services.json");
            return;
        }
        final String hash;
        try {
            hash = sha256Hex(pushToken);
        } catch (Exception e) {
            call.reject("Could not hash the push token", e);
            return;
        }
        request(call, project, hash, true);
    }

    private void request(PluginCall call, long project, String hash, boolean mayRetry) {
        withProvider(project, call, p -> p
            .request(StandardIntegrityTokenRequest.builder().setRequestHash(hash).build())
            .addOnSuccessListener(response -> resolve(call, response))
            .addOnFailureListener(e -> {
                // A provider goes stale (Play Services updates, a long idle):
                // prepare a new one and ask again, once.
                if (mayRetry && e instanceof StandardIntegrityException
                    && ((StandardIntegrityException) e).getErrorCode()
                        == StandardIntegrityErrorCode.INTEGRITY_TOKEN_PROVIDER_INVALID) {
                    provider = null;
                    request(call, project, hash, false);
                    return;
                }
                call.reject("Integrity token request failed: " + e.getMessage(), e);
            }));
    }

    private interface WithProvider {
        void run(StandardIntegrityTokenProvider p);
    }

    private void withProvider(long project, PluginCall call, WithProvider next) {
        if (provider != null) {
            next.run(provider);
            return;
        }
        StandardIntegrityManager manager = IntegrityManagerFactory.createStandard(getContext());
        Task<StandardIntegrityTokenProvider> prepared = manager.prepareIntegrityToken(
            PrepareIntegrityTokenRequest.builder().setCloudProjectNumber(project).build());
        prepared
            .addOnSuccessListener(p -> {
                provider = p;
                next.run(p);
            })
            .addOnFailureListener(e ->
                call.reject("Integrity provider could not be prepared: " + e.getMessage(), e));
    }

    private void resolve(PluginCall call, StandardIntegrityToken response) {
        JSObject ret = new JSObject();
        ret.put("token", response.token());
        call.resolve(ret);
    }

    /**
     * The Google Cloud project number the Play Console links to this app. A
     * Firebase project's sender ID is its project number, and the google-services
     * Gradle plugin writes it as the string resource gcm_defaultSenderId from
     * google-services.json, so it needs no second copy in the source. 0 when
     * the build has no google-services.json (the same builds PushGatePlugin
     * reports as having no push).
     */
    private long cloudProjectNumber() {
        Context context = getContext();
        int id = context.getResources().getIdentifier("gcm_defaultSenderId", "string", context.getPackageName());
        if (id == 0) return 0;
        try {
            return Long.parseLong(context.getString(id).trim());
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static String sha256Hex(String text) throws Exception {
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        StringBuilder hex = new StringBuilder(digest.length * 2);
        for (byte b : digest) hex.append(String.format("%02x", b));
        return hex.toString();
    }
}
