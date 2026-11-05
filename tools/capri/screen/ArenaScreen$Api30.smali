.class final Lapp/capri/ArenaScreen$Api30;
.super Ljava/lang/Object;
.source "ArenaScreen.java"


# annotations
.annotation system Ldalvik/annotation/EnclosingClass;
    value = Lapp/capri/ArenaScreen;
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x1a
    name = "Api30"
.end annotation

.annotation system Ldalvik/annotation/MemberClasses;
    value = {
        Lapp/capri/ArenaScreen$Api30$Capture;
    }
.end annotation


# direct methods
.method private constructor <init>()V
    .locals 0

    .line 101
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    return-void
.end method

.method static request(Lapp/capri/TypeWatch;Ljava/lang/String;Lkotlin/jvm/functions/Function1;Ljava/util/ArrayList;)V
    .locals 3
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "(",
            "Lapp/capri/TypeWatch;",
            "Ljava/lang/String;",
            "Lkotlin/jvm/functions/Function1;",
            "Ljava/util/ArrayList<",
            "Landroid/graphics/Rect;",
            ">;)V"
        }
    .end annotation

    .line 103
    new-instance v0, Lapp/capri/ArenaScreen$Api30$Capture;

    invoke-direct {v0, p1, p2, p3}, Lapp/capri/ArenaScreen$Api30$Capture;-><init>(Ljava/lang/String;Lkotlin/jvm/functions/Function1;Ljava/util/ArrayList;)V

    .line 104
    invoke-static {}, Lapp/capri/ArenaScreen;->access$3()Landroid/os/Handler;

    move-result-object p1

    new-instance p2, Lapp/capri/ArenaScreen$Api30$1;

    invoke-direct {p2, v0}, Lapp/capri/ArenaScreen$Api30$1;-><init>(Lapp/capri/ArenaScreen$Api30$Capture;)V

    .line 106
    nop

    .line 104
    const-wide/16 v1, 0x3a98

    invoke-virtual {p1, p2, v1, v2}, Landroid/os/Handler;->postDelayed(Ljava/lang/Runnable;J)Z

    .line 108
    const/4 p1, 0x0

    :try_start_0
    invoke-static {}, Lapp/capri/ArenaScreen;->access$5()Ljava/util/concurrent/ExecutorService;

    move-result-object p2

    invoke-virtual {p0, p1, p2, v0}, Lapp/capri/TypeWatch;->takeScreenshot(ILjava/util/concurrent/Executor;Landroid/accessibilityservice/AccessibilityService$TakeScreenshotCallback;)V
    :try_end_0
    .catchall {:try_start_0 .. :try_end_0} :catchall_0

    .line 109
    goto :goto_0

    :catchall_0
    move-exception p0

    .line 110
    const-string p0, "Screenshot unavailable: Android denied accessibility capture."

    invoke-virtual {v0, p0}, Lapp/capri/ArenaScreen$Api30$Capture;->fail(Ljava/lang/String;)V

    .line 112
    :goto_0
    return-void
.end method
