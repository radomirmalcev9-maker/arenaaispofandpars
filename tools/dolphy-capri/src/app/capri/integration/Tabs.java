package app.capri.integration;

import android.app.Activity;
import android.app.Application;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.lang.ref.WeakReference;

/** Two top-level tabs around the existing, unmodified Activity UIs. */
public final class Tabs implements Application.ActivityLifecycleCallbacks {
    private static final String CAPRI = "app.capri.MainActivity";
    private static final String DOLPHY = "com.droid.dolphy.MainActivity";
    private static final String ROUTE_EXTRA = "com.droid.dolphy.extra.NAV_ROUTE";
    private static final String TAG = "capri.dolphy.tabs.v1";
    private static volatile boolean installed;
    private static volatile boolean dolphyForeground;
    private static volatile WeakReference<Activity> foreground = new WeakReference<>(null);
    private static Application app;

    private Tabs() {}

    public static synchronized void install(Application application) {
        if (installed) return;
        app = application;
        application.registerActivityLifecycleCallbacks(new Tabs());
        installed = true;
    }

    static Context context() { return app; }
    static Activity activity() { return foreground.get(); }

    /** Only the focused Dolphy window is eligible; never another own-app window. */
    public static boolean isOwnNodeBlocked(Context context, Object packageName, AccessibilityNodeInfo node) {
        if (!isOwnPackage(context, packageName)) return false;
        return node == null || !isFocusedDolphyWindow(node.getWindowId());
    }

    public static boolean isOwnEventBlocked(Context context, Object packageName, AccessibilityEvent event) {
        if (!isOwnPackage(context, packageName)) return false;
        return event == null || !isFocusedDolphyWindow(event.getWindowId());
    }

    /** The event source can belong to a different window than the active root. */
    public static boolean isOwnRootOrEventBlocked(Context context, Object rootPackage,
                                                  AccessibilityNodeInfo root, AccessibilityEvent event) {
        if (isOwnNodeBlocked(context, rootPackage, root)) return true;
        return event != null && isOwnEventBlocked(context, event.getPackageName(), event);
    }

    private static boolean isOwnPackage(Context context, Object packageName) {
        return context != null && packageName != null && context.getPackageName().equals(packageName.toString());
    }

    private static boolean isFocusedDolphyWindow(int windowId) {
        Activity activity = foreground.get();
        if (!dolphyForeground || activity == null || !activity.hasWindowFocus() || windowId < 0) return false;
        AccessibilityNodeInfo root = null;
        try {
            root = activity.getWindow().getDecorView().createAccessibilityNodeInfo();
            return root != null && root.getWindowId() == windowId;
        } catch (Throwable ignored) {
            return false; // Fail closed if the window is not attached or accessible.
        } finally {
            if (root != null) root.recycle();
        }
    }

    static void openDolphy(String route) {
        Context source = activity();
        if (source == null) source = app;
        if (source == null) throw new IllegalStateException("Application not ready");
        Intent intent = new Intent();
        intent.setClassName(source, DOLPHY);
        intent.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (!(source instanceof Activity)) intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (route != null && !route.isEmpty()) intent.putExtra(ROUTE_EXTRA, route);
        source.startActivity(intent);
    }

    private static void openCapri(Activity source) {
        Intent intent = new Intent();
        intent.setClassName(source, CAPRI);
        intent.addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        source.startActivity(intent);
    }

    @Override public void onActivityCreated(final Activity activity, Bundle saved) {
        final String name = activity.getClass().getName();
        if (CAPRI.equals(name) || DOLPHY.equals(name)) {
            // onActivityCreated is dispatched after Activity.onCreate/setContentView,
            // but before the window is normally attached. Keep the existing view and state.
            if (!wrap(activity, CAPRI.equals(name))) {
                activity.getWindow().getDecorView().post(new Runnable() {
                    @Override public void run() { wrap(activity, CAPRI.equals(name)); }
                });
            }
        }
    }

    private static boolean wrap(final Activity activity, boolean capriSelected) {
        if (activity.isFinishing()) return true;
        FrameLayout content = activity.findViewById(android.R.id.content);
        if (content == null) return false;
        View tagged = content.findViewWithTag(TAG);
        if (tagged != null) return true;
        if (content.getChildCount() != 1) return false;
        View original = content.getChildAt(0);
        content.removeView(original);
        try {
            LinearLayout container = new LinearLayout(activity);
            container.setTag(TAG);
            container.setOrientation(LinearLayout.VERTICAL);
            container.setBackgroundColor(Color.rgb(15, 19, 28));

            LinearLayout bar = new LinearLayout(activity);
            bar.setOrientation(LinearLayout.HORIZONTAL);
            bar.setBackgroundColor(Color.rgb(18, 23, 34));
            int height = Math.round(48 * activity.getResources().getDisplayMetrics().density);
            container.addView(bar, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, height));
            bar.addView(tab(activity, "Capri", capriSelected, new View.OnClickListener() {
                @Override public void onClick(View v) { openCapri(activity); }
            }), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1));
            bar.addView(tab(activity, "Dolphy", !capriSelected, new View.OnClickListener() {
                @Override public void onClick(View v) { openDolphy(null); }
            }), new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1));
            container.addView(original, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            content.addView(container, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            return true;
        } catch (Throwable error) {
            // Never leave the original UI detached if adding the bar fails.
            ViewGroup parent = (ViewGroup) original.getParent();
            if (parent != null) parent.removeView(original);
            content.removeAllViews();
            content.addView(original, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            return true;
        }
    }

    private static TextView tab(Activity activity, String name, boolean selected, View.OnClickListener click) {
        TextView tab = new TextView(activity);
        tab.setText(name);
        tab.setTextSize(14);
        tab.setGravity(Gravity.CENTER);
        tab.setTypeface(Typeface.DEFAULT, selected ? Typeface.BOLD : Typeface.NORMAL);
        tab.setTextColor(selected ? Color.WHITE : Color.rgb(167, 176, 193));
        tab.setBackgroundColor(selected ? Color.rgb(42, 58, 78) : Color.TRANSPARENT);
        tab.setContentDescription(name + (selected ? ", выбрана" : ", вкладка"));
        tab.setClickable(true);
        tab.setOnClickListener(click);
        return tab;
    }

    @Override public void onActivityResumed(Activity activity) {
        foreground = new WeakReference<>(activity);
        dolphyForeground = activity.getClass().getName().startsWith("com.droid.dolphy.");
    }

    @Override public void onActivityPaused(Activity activity) {
        if (foreground.get() == activity) {
            dolphyForeground = false;
            foreground = new WeakReference<>(null);
        }
    }
    @Override public void onActivityStarted(Activity activity) {}
    @Override public void onActivityStopped(Activity activity) {}
    @Override public void onActivitySaveInstanceState(Activity activity, Bundle outState) {}
    @Override public void onActivityDestroyed(Activity activity) {
        if (foreground.get() == activity) {
            dolphyForeground = false;
            foreground = new WeakReference<>(null);
        }
    }
}
