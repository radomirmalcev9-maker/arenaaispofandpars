package android.view.accessibility;
public class AccessibilityEvent {
    private final int id;
    private final CharSequence packageName;
    public AccessibilityEvent(int id) { this(id, "com.droid.dolphy.capri"); }
    public AccessibilityEvent(int id, CharSequence name) { this.id = id; this.packageName = name; }
    public int getWindowId() { return id; }
    public CharSequence getPackageName() { return packageName; }
}
