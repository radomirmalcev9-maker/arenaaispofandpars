package android.os;
import java.util.ArrayList;
import java.util.List;
public class Handler {
    private static final List<Runnable> delayed = new ArrayList<>();
    public Handler(Looper looper) {}
    public boolean post(Runnable task) { task.run(); return true; }
    public boolean postDelayed(Runnable task, long delay) { synchronized (delayed) { delayed.add(task); } return true; }
    public static void runDelayed() {
        List<Runnable> tasks;
        synchronized (delayed) { tasks = new ArrayList<>(delayed); delayed.clear(); }
        for (Runnable task : tasks) task.run();
    }
}
