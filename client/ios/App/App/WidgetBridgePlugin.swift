import Foundation
import Capacitor
import WidgetKit

// What the page tells the home-screen widget (client/www/widget.js): which
// cities to show, in which language, with which words. One JSON string, kept in
// the App Group's UserDefaults, which the widget extension reads. No credential
// goes through here: the widget reads the device credential from the shared
// Keychain group itself (SecureStorePlugin.swift). Write-only from the page's
// side, and nothing leaves the phone.
//
// `group` and the keys are repeated in BuergerweckerWidget.swift; the
// extension is a separate target and shares no source with the app.
// client/test/widget.test.mjs holds the two copies to each other.
@objc(WidgetBridgePlugin)
public class WidgetBridgePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "WidgetBridgePlugin"
    public let jsName = "WidgetBridge"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "setConfig", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clear", returnType: CAPPluginReturnPromise),
    ]

    private static let group = "group.de.buergerwecker.app"
    private static let configKey = "widget_config"
    private static let cacheKey = "widget_cache"
    private static let lockedKey = "widget_locked"

    @objc func setConfig(_ call: CAPPluginCall) {
        guard let config = call.getString("config") else { call.reject("config missing"); return }
        guard let defaults = UserDefaults(suiteName: Self.group) else { call.reject("App Group unavailable"); return }
        defaults.set(config, forKey: Self.configKey)
        WidgetCenter.shared.reloadAllTimelines()
        call.resolve()
    }

    // "Delete my data": the list and whatever the widget last fetched.
    @objc func clear(_ call: CAPPluginCall) {
        let defaults = UserDefaults(suiteName: Self.group)
        defaults?.removeObject(forKey: Self.configKey)
        defaults?.removeObject(forKey: Self.cacheKey)
        defaults?.removeObject(forKey: Self.lockedKey)
        WidgetCenter.shared.reloadAllTimelines()
        call.resolve()
    }
}
