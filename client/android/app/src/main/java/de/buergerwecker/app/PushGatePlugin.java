package de.buergerwecker.app;

import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Two things the push plugin does not offer.
 *
 * status(): whether this build can receive pushes at all. The google-services
 * Gradle plugin writes the string resource google_app_id from
 * google-services.json; a build made without that file (every local and PR
 * build until the Firebase project exists) has none, and there
 * PushNotifications.register() would throw on an uninitialised FirebaseApp and
 * take the app down. The page asks first (client/www/push.js) and treats a
 * missing plugin, as on iOS, as "available".
 *
 * openSettings(): the system page where this app's notifications are switched
 * on, for the "Open Settings" button while permission is denied
 * (client/www/native.js). Android has no app-settings: URL as iOS does.
 */
@CapacitorPlugin(name = "PushGate")
public class PushGatePlugin extends Plugin {
    @PluginMethod
    public void status(PluginCall call) {
        int id = getContext().getResources().getIdentifier("google_app_id", "string", getContext().getPackageName());
        JSObject ret = new JSObject();
        ret.put("available", id != 0);
        call.resolve(ret);
    }

    @PluginMethod
    public void openSettings(PluginCall call) {
        String pkg = getContext().getPackageName();
        Intent intent = null;
        // The notification page itself exists from API 26; minSdk is 24, so
        // older phones (and any build where it cannot be resolved) get the
        // app's details page, one tap away from it.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Intent notifications = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
                .putExtra(Settings.EXTRA_APP_PACKAGE, pkg);
            if (notifications.resolveActivity(getContext().getPackageManager()) != null) intent = notifications;
        }
        if (intent == null) {
            intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", pkg, null));
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            getContext().startActivity(intent);
        } catch (Exception e) {
            call.reject("No settings page to open", e);
            return;
        }
        call.resolve();
    }
}
