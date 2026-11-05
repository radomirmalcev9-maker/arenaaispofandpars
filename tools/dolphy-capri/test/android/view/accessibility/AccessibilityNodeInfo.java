package android.view.accessibility;
public class AccessibilityNodeInfo {
    private final int id;
    public AccessibilityNodeInfo(int id) { this.id = id; }
    public int getWindowId() { return id; }
    public void recycle() {}
}
