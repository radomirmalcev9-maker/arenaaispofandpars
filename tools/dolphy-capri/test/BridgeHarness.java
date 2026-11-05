import android.app.Activity;
import android.app.AlertDialog;
import android.os.Handler;
import app.capri.integration.DolphyBridge;
import app.capri.integration.Tabs;
import capriembed.kotlin.jvm.functions.Function1;
import com.droid.dolphy.plugin.bridge.PluginDeviceApis;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

public class BridgeHarness {
    public static final class Call {
        private final String name; private final List<String> args;
        Call(String name, String... args) { this.name = name; this.args = Arrays.asList(args); }
        public String getName() { return name; }
        public List<String> getArgs() { return args; }
    }
    public static final class Result implements Function1 {
        final CountDownLatch latch = new CountDownLatch(1);
        volatile String text; volatile int count;
        public Object invoke(Object value) { text = String.valueOf(value); count++; latch.countDown(); return null; }
        String await() throws Exception { if (!latch.await(5, TimeUnit.SECONDS)) throw new AssertionError("no callback"); return text; }
    }
    static void check(boolean condition, String message) { if (!condition) throw new AssertionError(message); }
    public static void main(String[] args) throws Exception {
        Result ordinary = new Result();
        check(!DolphyBridge.intercept(new Call("open", "camera"), null, ordinary), "unrelated open consumed");
        check(DolphyBridge.intercept(new Call("open", "dolphy"), null, ordinary), "open dolphy not handled");
        check(Tabs.opened == 1 && ordinary.await().contains("Opening"), "Dolphy did not open");

        Result invalid = new Result();
        check(DolphyBridge.intercept(new Call("dolphy", "open", "not_a_route"), null, invalid), "route unhandled");
        check(invalid.await().contains("Unknown") && Tabs.opened == 1, "unknown route reached navigation");
        Result valid = new Result();
        DolphyBridge.intercept(new Call("dolphy", "open", "other"), null, valid);
        check(valid.await().contains("Opening") && "other".equals(Tabs.lastRoute), "allowed route rejected");

        Result methods = new Result();
        DolphyBridge.intercept(new Call("dolphy", "list", "device", "ir"), null, methods);
        check(methods.await().contains("irStatusJson"), "method discovery failed");

        Result denied = new Result();
        DolphyBridge.intercept(new Call("dolphy", "device", "irStatusJson", "[]"), null, denied);
        check(PluginDeviceApis.calls == 0, "executed before confirmation");
        check(AlertDialog.current.message.contains("device.irStatusJson"), "confirmation hides method");
        AlertDialog.current.reject();
        check(denied.await().contains("cancelled") && PluginDeviceApis.calls == 0, "cancel executed an action");

        Result approved = new Result();
        DolphyBridge.intercept(new Call("dolphy", "device", "irStatusJson", "[]"), null, approved);
        check(PluginDeviceApis.calls == 0, "pre-approval execution");
        AlertDialog.current.approve();
        check(approved.await().contains("ir-ok") && PluginDeviceApis.calls == 1, "approved result missing");
        Handler.runDelayed();
        check(approved.count == 1 && denied.count == 1, "completion delivered more than once");

        Result async = new Result();
        DolphyBridge.intercept(new Call("dolphy", "device", "asyncThing", "[\"hello\"]"), null, async);
        AlertDialog.current.approve();
        check(async.await().contains("answer:hello") && PluginDeviceApis.calls == 2, "async callback bridge failed");
        Handler.runDelayed();
        check(async.count == 1, "async callback repeated after timeout");

        Result malformed = new Result();
        DolphyBridge.intercept(new Call("dolphy", "device", "setFlag", "[1]"), null, malformed);
        check(malformed.await().contains("No supported overload") && PluginDeviceApis.calls == 2, "bad argument accepted");

        Tabs.foreground = null;
        Result background = new Result();
        DolphyBridge.intercept(new Call("dolphy", "device", "irStatusJson", "[]"), null, background);
        check(background.await().contains("Cannot ask for confirmation") && PluginDeviceApis.calls == 2, "background call executed");
        Tabs.foreground = new Activity();
        System.out.println("BridgeHarness: all checks passed");
        System.exit(0); // Stop the test's cached worker pool.
    }
}
