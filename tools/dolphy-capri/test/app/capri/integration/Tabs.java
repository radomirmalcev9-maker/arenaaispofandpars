package app.capri.integration;
import android.app.Activity;
import android.content.Context;
public class Tabs {
    public static Activity foreground = new Activity();
    public static int opened;
    public static String lastRoute;
    static Context context() { return foreground == null ? new Context() : foreground; }
    static Activity activity() { return foreground; }
    static void openDolphy(String route) { opened++; lastRoute = route; }
}
