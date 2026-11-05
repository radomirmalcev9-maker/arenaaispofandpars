import android.app.Application;
import android.content.Context;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import app.capri.integration.Tabs;

public class TabsHarness {
    static void check(boolean ok, String message) { if (!ok) throw new AssertionError(message); }
    public static void main(String[] args) {
        Application app = new Application();
        Tabs.install(app);
        Tabs.install(app);  // idempotent
        Context ctx = new Context();
        String own = ctx.getPackageName();
        AccessibilityNodeInfo window7 = new AccessibilityNodeInfo(7);
        AccessibilityNodeInfo window8 = new AccessibilityNodeInfo(8);
        check(Tabs.isOwnNodeBlocked(ctx, own, window7), "own screen allowed before resume");
        check(!Tabs.isOwnNodeBlocked(ctx, "other.app", window7), "external screen blocked");
        com.droid.dolphy.MainActivity dolphy = new com.droid.dolphy.MainActivity();
        app.callbacks.onActivityResumed(dolphy);
        check(!Tabs.isOwnNodeBlocked(ctx, own, window7), "focused Dolphy node blocked");
        check(!Tabs.isOwnEventBlocked(ctx, own, new AccessibilityEvent(7)), "focused Dolphy event blocked");
        check(Tabs.isOwnNodeBlocked(ctx, own, window8), "other own window exposed");
        check(Tabs.isOwnEventBlocked(ctx, own, new AccessibilityEvent(8)), "other own event exposed");
        check(Tabs.isOwnRootOrEventBlocked(ctx, "external", window7, new AccessibilityEvent(8, own)), "own event exposed through external root");
        check(Tabs.isOwnRootOrEventBlocked(ctx, own, window8, new AccessibilityEvent(7, "external")), "other own root exposed through external event");
        check(!Tabs.isOwnRootOrEventBlocked(ctx, own, window7, new AccessibilityEvent(7, own)), "focused Dolphy root/event blocked");
        check(Tabs.isOwnNodeBlocked(ctx, own, null), "null node exposed");
        dolphy.focused = false;
        check(Tabs.isOwnNodeBlocked(ctx, own, window7), "unfocused Dolphy window exposed");
        dolphy.focused = true;
        app.callbacks.onActivityPaused(dolphy);
        check(Tabs.isOwnNodeBlocked(ctx, own, window7), "paused Dolphy window exposed");
        app.callbacks.onActivityResumed(new app.capri.MainActivity());
        check(Tabs.isOwnNodeBlocked(ctx, own, window7), "Capri window exposed");
        System.out.println("TabsHarness: all checks passed");
    }
}
