package com.droid.dolphy.plugin.bridge;
import android.content.Context;
import kotlin.jvm.functions.Function1;
public class PluginDeviceApis {
    public static volatile int calls;
    public PluginDeviceApis(Context context, String pluginId) {}
    public String irStatusJson() { calls++; return "ir-ok"; }
    public void asyncThing(String argument, Function1 callback) { calls++; callback.invoke("answer:" + argument); }
    public boolean setFlag(boolean value) { calls++; return value; }
}
