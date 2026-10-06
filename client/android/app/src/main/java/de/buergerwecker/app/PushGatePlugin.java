package de.buergerwecker.app;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Whether this build can receive pushes at all. The google-services Gradle
 * plugin writes the string resource google_app_id from google-services.json;
 * a build made without that file (every local and PR build until the Firebase
 * project exists) has none, and there PushNotifications.register() would throw
 * on an uninitialised FirebaseApp and take the app down. The page asks first
 * (client/www/push.js) and treats a missing plugin, as on iOS, as "available".
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
}
