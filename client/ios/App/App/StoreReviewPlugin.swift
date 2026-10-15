import Capacitor
import StoreKit

// Asks the App Store for its rating sheet (client/www/native.js, requestReview),
// called when the reader answers the check-in with "No, I've got one". StoreKit
// decides whether the sheet shows (never in TestFlight, at most three times a
// year) and says nothing back, so the call always resolves.
@objc(StoreReviewPlugin)
public class StoreReviewPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "StoreReviewPlugin"
    public let jsName = "StoreReview"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "requestReview", returnType: CAPPluginReturnPromise),
    ]

    @objc func requestReview(_ call: CAPPluginCall) {
        Task { @MainActor in
            if let scene = self.bridge?.viewController?.view.window?.windowScene {
                AppStore.requestReview(in: scene)
            }
            call.resolve()
        }
    }
}
