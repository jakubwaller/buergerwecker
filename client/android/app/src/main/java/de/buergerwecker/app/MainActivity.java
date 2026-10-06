package de.buergerwecker.app;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Before super: the bridge is built there, with the plugins it knows.
        registerPlugin(PushGatePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
