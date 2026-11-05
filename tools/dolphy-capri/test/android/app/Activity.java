package android.app;
import android.view.View;
import android.view.Window;
public class Activity extends android.content.Context {
    public boolean focused = true;
    private final Window window = new Window();
    public boolean isFinishing() { return false; }
    public boolean hasWindowFocus() { return focused; }
    public Window getWindow() { return window; }
    @SuppressWarnings("unchecked") public <T extends View> T findViewById(int id) { return (T) new android.widget.FrameLayout(); }
}
