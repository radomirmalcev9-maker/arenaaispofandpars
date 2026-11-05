package android.app;
import android.content.DialogInterface;
public class AlertDialog implements DialogInterface {
    public static AlertDialog current;
    private OnClickListener negative, positive;
    private OnCancelListener cancel;
    private OnDismissListener dismiss;
    private boolean visible;
    public String message;
    public void show() { current = this; visible = true; }
    public boolean isShowing() { return visible; }
    public void dismiss() { visible = false; if (dismiss != null) dismiss.onDismiss(this); }
    public void approve() { positive.onClick(this, 1); dismiss(); }
    public void reject() { negative.onClick(this, 0); dismiss(); }
    public void cancel() { if (cancel != null) cancel.onCancel(this); dismiss(); }
    public static class Builder {
        private final AlertDialog dialog = new AlertDialog();
        public Builder(Activity activity) {}
        public Builder setTitle(String text) { return this; }
        public Builder setMessage(String text) { dialog.message = text; return this; }
        public Builder setNegativeButton(String text, OnClickListener listener) { dialog.negative = listener; return this; }
        public Builder setPositiveButton(String text, OnClickListener listener) { dialog.positive = listener; return this; }
        public Builder setOnCancelListener(OnCancelListener listener) { dialog.cancel = listener; return this; }
        public Builder setOnDismissListener(OnDismissListener listener) { dialog.dismiss = listener; return this; }
        public AlertDialog create() { return dialog; }
    }
}
