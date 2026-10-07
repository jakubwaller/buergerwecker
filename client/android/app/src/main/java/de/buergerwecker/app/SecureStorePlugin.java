package de.buergerwecker.app;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * The page's side of {@link SecureStore}: the device credential as one JSON
 * text (client/www/store.js), never in Capacitor's Preferences. Every change
 * asks the widget to update, since it is what the widget fetches with: a new
 * credential lets a widget that showed "open the app" show slots again, and a
 * removed one stops it. client/test/widget.test.mjs holds every method the page
 * calls to a @PluginMethod here and to its iOS twin.
 */
@CapacitorPlugin(name = "SecureStore")
public class SecureStorePlugin extends Plugin {
    /** { value: "<json>" }, or {} with nothing stored. */
    @PluginMethod
    public void get(PluginCall call) {
        JSObject ret = new JSObject();
        try {
            String value = SecureStore.read(getContext());
            if (value != null) ret.put("value", value);
        } catch (SecureStore.UnreadableException e) {
            call.reject("credential unreadable", e);
            return;
        }
        call.resolve(ret);
    }

    @PluginMethod
    public void set(PluginCall call) {
        String value = call.getString("value");
        if (value == null || value.isEmpty()) {
            call.reject("value missing");
            return;
        }
        try {
            SecureStore.write(getContext(), value);
        } catch (Exception e) {
            call.reject("could not store the credential", e);
            return;
        }
        WidgetBridgePlugin.requestUpdate(getContext());
        call.resolve();
    }

    // "Delete my data", a device the server dropped.
    @PluginMethod
    public void clear(PluginCall call) {
        SecureStore.clear(getContext());
        WidgetBridgePlugin.requestUpdate(getContext());
        call.resolve();
    }
}
