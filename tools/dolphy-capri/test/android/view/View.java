package android.view;
import android.view.accessibility.AccessibilityNodeInfo;
public class View {
    public interface OnClickListener { void onClick(View view); }
    public int windowId = 7;
    public AccessibilityNodeInfo createAccessibilityNodeInfo() { return new AccessibilityNodeInfo(windowId); }
    public boolean post(Runnable task) { task.run(); return true; }
    public Object getParent() { return null; }
    public void setTag(Object tag) {}
}
