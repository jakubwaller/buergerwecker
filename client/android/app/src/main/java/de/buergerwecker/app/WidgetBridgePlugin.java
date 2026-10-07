package de.buergerwecker.app;

import android.appwidget.AppWidgetManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;

import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * What the page tells the home-screen widget (client/www/widget.js): the cities
 * to show, the language and the words, as one JSON string in
 * SharedPreferences, which EarliestSlotWidget reads. No credential goes through
 * here: the widget reads the device credential from SecureStore itself. After a
 * change the widgets are asked to update at once instead of waiting for their
 * 30-minute period. Write-only from the page's side; nothing leaves the phone.
 *
 * The preference names live in EarliestSlotWidget; client/test/widget.test.mjs
 * holds every method the page calls to a @PluginMethod here.
 */
@CapacitorPlugin(name = "WidgetBridge")
public class WidgetBridgePlugin extends Plugin {
    @PluginMethod
    public void setConfig(PluginCall call) {
        String config = call.getString("config");
        if (config == null) {
            call.reject("config missing");
            return;
        }
        getContext().getSharedPreferences(EarliestSlotWidget.PREFS, Context.MODE_PRIVATE)
            .edit().putString(EarliestSlotWidget.KEY_CONFIG, config).apply();
        requestUpdate(getContext());
        call.resolve();
    }

    // "Delete my data": the list and whatever the widget last fetched.
    @PluginMethod
    public void clear(PluginCall call) {
        getContext().getSharedPreferences(EarliestSlotWidget.PREFS, Context.MODE_PRIVATE)
            .edit().remove(EarliestSlotWidget.KEY_CONFIG).remove(EarliestSlotWidget.KEY_CACHE)
            .remove(EarliestSlotWidget.KEY_LEGACY_CACHE).remove(EarliestSlotWidget.KEY_LOCKED).apply();
        requestUpdate(getContext());
        call.resolve();
    }

    /** Also SecureStorePlugin's, when the credential changes. */
    static void requestUpdate(Context context) {
        AppWidgetManager manager = AppWidgetManager.getInstance(context);
        int[] ids = manager.getAppWidgetIds(new ComponentName(context, EarliestSlotWidget.class));
        if (ids.length == 0) return;
        context.sendBroadcast(new Intent(AppWidgetManager.ACTION_APPWIDGET_UPDATE)
            .setClass(context, EarliestSlotWidget.class)
            .putExtra(AppWidgetManager.EXTRA_APPWIDGET_IDS, ids));
    }
}
