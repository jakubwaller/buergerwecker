import UIKit
import Capacitor

// SceneDelegate puts this in the window instead of a bare CAPBridgeViewController,
// so the app's own plugins are on the bridge before the page loads. An app-target
// plugin has no package for the Capacitor CLI to discover; they are registered here.
class MainViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(WidgetBridgePlugin())
        bridge?.registerPluginInstance(SecureStorePlugin())
    }
}
