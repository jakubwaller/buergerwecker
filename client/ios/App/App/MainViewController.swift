import UIKit
import Capacitor

// SceneDelegate puts this in the window instead of a bare CAPBridgeViewController,
// so the app's own plugin is on the bridge before the page loads. An app-target
// plugin has no package for the Capacitor CLI to discover; it is registered here.
class MainViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(WidgetBridgePlugin())
    }
}
