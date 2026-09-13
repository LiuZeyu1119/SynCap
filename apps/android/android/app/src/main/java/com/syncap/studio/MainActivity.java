package com.syncap.studio;

import android.os.Bundle;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(SynCapDevicePlugin.class);
        super.onCreate(savedInstanceState);
    }
}
