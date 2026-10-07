import Foundation
import Capacitor
import Security
import WidgetKit

// The device credential (`{"id": …, "secret": …}`, the JSON text www/store.js
// hands over) in the Keychain, not in Preferences: UserDefaults go into iCloud
// and Finder backups, and with the credential anyone holding a backup could
// read and delete the person's alerts.
//
// - AfterFirstUnlockThisDeviceOnly: readable once the phone has been unlocked
//   after a restart (the widget refreshes in the background, often locked),
//   never synchronised to iCloud Keychain, never restored onto another phone.
// - In the keychain access group `<team>.de.buergerwecker.shared`, which both
//   targets list under keychain-access-groups in their entitlements, so the
//   widget extension can read it for its GET /cities/<slug>/slots
//   (`Credential` in BuergerweckerWidget.swift repeats `service` and
//   `account`; client/test/widget.test.mjs holds the copies to each other).
//   With that entitlement the group is also the app's default one, so a read
//   names no group and finds the item wherever the app may look.
//
// Every change reloads the widget: a new credential is what lets a widget that
// showed "open the app" show slots again, and a removed one must stop it.
@objc(SecureStorePlugin)
public class SecureStorePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "SecureStorePlugin"
    public let jsName = "SecureStore"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "get", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "set", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clear", returnType: CAPPluginReturnPromise),
    ]

    // { value: "<json>" }, or {} with nothing stored.
    @objc func get(_ call: CAPPluginCall) {
        if let value = DeviceKeychain.read() { call.resolve(["value": value]) } else { call.resolve([:]) }
    }

    @objc func set(_ call: CAPPluginCall) {
        guard let value = call.getString("value"), !value.isEmpty else { call.reject("value missing"); return }
        let status = DeviceKeychain.write(value)
        guard status == errSecSuccess else { call.reject("Keychain write failed (\(status))"); return }
        WidgetCenter.shared.reloadAllTimelines()
        call.resolve()
    }

    // "Delete my data", a device the server dropped.
    @objc func clear(_ call: CAPPluginCall) {
        let status = DeviceKeychain.delete()
        WidgetCenter.shared.reloadAllTimelines()
        guard status == errSecSuccess else { call.reject("Keychain delete failed (\(status))"); return }
        call.resolve()
    }
}

enum DeviceKeychain {
    static let service = "de.buergerwecker.device"
    static let account = "credential"
    // After the team prefix; the entitlements say $(AppIdentifierPrefix)de.buergerwecker.shared.
    static let groupName = "de.buergerwecker.shared"

    // "<TEAMID>.de.buergerwecker.shared": Info.plist's AppIdentifierPrefix is
    // $(AppIdentifierPrefix), which the build fills in from the team. nil in a
    // build without one (the unsigned CI build), where the default group,
    // the first of keychain-access-groups, is the same one anyway.
    static var accessGroup: String? {
        guard let prefix = Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String,
              prefix.count > 1, prefix.hasSuffix("."), !prefix.contains("$") else { return nil }
        return prefix + groupName
    }

    // What identifies the one item, in every group the app can reach.
    private static var match: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account,
         kSecAttrSynchronizable as String: false]
    }

    static func read() -> String? {
        var query = match
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &out) == errSecSuccess,
              let data = out as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func write(_ value: String) -> OSStatus {
        let fields: [String: Any] = [
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]
        let updated = SecItemUpdate(match as CFDictionary, fields as CFDictionary)
        guard updated == errSecItemNotFound else { return updated }
        var add = match.merging(fields) { _, new in new }
        if let group = accessGroup { add[kSecAttrAccessGroup as String] = group }
        var status = SecItemAdd(add as CFDictionary, nil)
        if status == errSecMissingEntitlement, add[kSecAttrAccessGroup as String] != nil {
            // Signed without the group (a local run under another team): the
            // default group still keeps it out of backups, the widget just
            // cannot read it there.
            add.removeValue(forKey: kSecAttrAccessGroup as String)
            status = SecItemAdd(add as CFDictionary, nil)
        }
        return status
    }

    static func delete() -> OSStatus {
        let status = SecItemDelete(match as CFDictionary)
        return status == errSecItemNotFound ? errSecSuccess : status
    }
}
