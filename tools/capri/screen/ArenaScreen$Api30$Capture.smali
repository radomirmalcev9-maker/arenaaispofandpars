.class final Lapp/capri/ArenaScreen$Api30$Capture;
.super Ljava/lang/Object;
.source "ArenaScreen.java"

# interfaces
.implements Landroid/accessibilityservice/AccessibilityService$TakeScreenshotCallback;


# annotations
.annotation system Ldalvik/annotation/EnclosingClass;
    value = Lapp/capri/ArenaScreen$Api30;
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x1a
    name = "Capture"
.end annotation


# instance fields
.field private final bounds:Ljava/util/ArrayList;
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "Ljava/util/ArrayList<",
            "Landroid/graphics/Rect;",
            ">;"
        }
    .end annotation
.end field

.field private final done:Lkotlin/jvm/functions/Function1;

.field private final finished:Ljava/util/concurrent/atomic/AtomicBoolean;

.field private final prefix:Ljava/lang/String;


# direct methods
.method constructor <init>(Ljava/lang/String;Lkotlin/jvm/functions/Function1;Ljava/util/ArrayList;)V
    .locals 1
    .annotation system Ldalvik/annotation/Signature;
        value = {
            "(",
            "Ljava/lang/String;",
            "Lkotlin/jvm/functions/Function1;",
            "Ljava/util/ArrayList<",
            "Landroid/graphics/Rect;",
            ">;)V"
        }
    .end annotation

    .line 120
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    .line 118
    new-instance v0, Ljava/util/concurrent/atomic/AtomicBoolean;

    invoke-direct {v0}, Ljava/util/concurrent/atomic/AtomicBoolean;-><init>()V

    iput-object v0, p0, Lapp/capri/ArenaScreen$Api30$Capture;->finished:Ljava/util/concurrent/atomic/AtomicBoolean;

    .line 121
    iput-object p1, p0, Lapp/capri/ArenaScreen$Api30$Capture;->prefix:Ljava/lang/String;

    .line 122
    iput-object p2, p0, Lapp/capri/ArenaScreen$Api30$Capture;->done:Lkotlin/jvm/functions/Function1;

    .line 123
    iput-object p3, p0, Lapp/capri/ArenaScreen$Api30$Capture;->bounds:Ljava/util/ArrayList;

    .line 124
    return-void
.end method

.method static synthetic access$0(Lapp/capri/ArenaScreen$Api30$Capture;)Lkotlin/jvm/functions/Function1;
    .locals 0

    .line 116
    iget-object p0, p0, Lapp/capri/ArenaScreen$Api30$Capture;->done:Lkotlin/jvm/functions/Function1;

    return-object p0
.end method

.method static synthetic access$1(Lapp/capri/ArenaScreen$Api30$Capture;)Ljava/lang/String;
    .locals 0

    .line 115
    iget-object p0, p0, Lapp/capri/ArenaScreen$Api30$Capture;->prefix:Ljava/lang/String;

    return-object p0
.end method


# virtual methods
.method fail(Ljava/lang/String;)V
    .locals 3

    .line 176
    iget-object v0, p0, Lapp/capri/ArenaScreen$Api30$Capture;->finished:Ljava/util/concurrent/atomic/AtomicBoolean;

    const/4 v1, 0x0

    const/4 v2, 0x1

    invoke-virtual {v0, v1, v2}, Ljava/util/concurrent/atomic/AtomicBoolean;->compareAndSet(ZZ)Z

    move-result v0

    if-nez v0, :cond_0

    return-void

    .line 177
    :cond_0
    invoke-static {}, Lapp/capri/ArenaScreen;->access$3()Landroid/os/Handler;

    move-result-object v0

    new-instance v1, Lapp/capri/ArenaScreen$Api30$Capture$2;

    invoke-direct {v1, p0, p1}, Lapp/capri/ArenaScreen$Api30$Capture$2;-><init>(Lapp/capri/ArenaScreen$Api30$Capture;Ljava/lang/String;)V

    invoke-virtual {v0, v1}, Landroid/os/Handler;->post(Ljava/lang/Runnable;)Z

    .line 180
    return-void
.end method

.method public onFailure(I)V
    .locals 2

    .line 171
    new-instance v0, Ljava/lang/StringBuilder;

    const-string v1, "Screenshot unavailable (Android error "

    invoke-direct {v0, v1}, Ljava/lang/StringBuilder;-><init>(Ljava/lang/String;)V

    invoke-virtual {v0, p1}, Ljava/lang/StringBuilder;->append(I)Ljava/lang/StringBuilder;

    .line 172
    const-string p1, "). Secure or protected screens may block capture."

    invoke-virtual {v0, p1}, Ljava/lang/StringBuilder;->append(Ljava/lang/String;)Ljava/lang/StringBuilder;

    invoke-virtual {v0}, Ljava/lang/StringBuilder;->toString()Ljava/lang/String;

    move-result-object p1

    .line 171
    invoke-virtual {p0, p1}, Lapp/capri/ArenaScreen$Api30$Capture;->fail(Ljava/lang/String;)V

    .line 173
    return-void
.end method

.method public onSuccess(Landroid/accessibilityservice/AccessibilityService$ScreenshotResult;)V
    .locals 8

    .line 127
    nop

    .line 128
    nop

    .line 129
    nop

    .line 131
    const/4 v0, 0x0

    :try_start_0
    invoke-virtual {p1}, Landroid/accessibilityservice/AccessibilityService$ScreenshotResult;->getHardwareBuffer()Landroid/hardware/HardwareBuffer;

    move-result-object v1
    :try_end_0
    .catchall {:try_start_0 .. :try_end_0} :catchall_2

    .line 132
    :try_start_1
    iget-object v2, p0, Lapp/capri/ArenaScreen$Api30$Capture;->finished:Ljava/util/concurrent/atomic/AtomicBoolean;

    invoke-virtual {v2}, Ljava/util/concurrent/atomic/AtomicBoolean;->get()Z

    move-result v2
    :try_end_1
    .catchall {:try_start_1 .. :try_end_1} :catchall_1

    if-eqz v2, :cond_1

    .line 164
    nop

    .line 165
    nop

    .line 166
    if-eqz v1, :cond_0

    invoke-virtual {v1}, Landroid/hardware/HardwareBuffer;->close()V

    .line 132
    :cond_0
    return-void

    .line 133
    :cond_1
    :try_start_2
    invoke-virtual {p1}, Landroid/accessibilityservice/AccessibilityService$ScreenshotResult;->getColorSpace()Landroid/graphics/ColorSpace;

    move-result-object p1

    invoke-static {v1, p1}, Landroid/graphics/Bitmap;->wrapHardwareBuffer(Landroid/hardware/HardwareBuffer;Landroid/graphics/ColorSpace;)Landroid/graphics/Bitmap;

    move-result-object p1
    :try_end_2
    .catchall {:try_start_2 .. :try_end_2} :catchall_1

    .line 134
    if-eqz p1, :cond_a

    .line 135
    :try_start_3
    sget-object v2, Landroid/graphics/Bitmap$Config;->ARGB_8888:Landroid/graphics/Bitmap$Config;

    const/4 v3, 0x1

    invoke-virtual {p1, v2, v3}, Landroid/graphics/Bitmap;->copy(Landroid/graphics/Bitmap$Config;Z)Landroid/graphics/Bitmap;

    move-result-object v0

    .line 136
    if-eqz v0, :cond_9

    .line 137
    invoke-virtual {v0}, Landroid/graphics/Bitmap;->getWidth()I

    move-result v2

    .line 138
    invoke-virtual {v0}, Landroid/graphics/Bitmap;->getHeight()I

    move-result v4

    .line 139
    invoke-static {v2, v4}, Ljava/lang/Math;->max(II)I

    move-result v5

    .line 140
    const/16 v6, 0x780

    if-le v5, v6, :cond_4

    .line 141
    const/high16 v6, 0x44f00000    # 1920.0f

    int-to-float v5, v5

    div-float/2addr v6, v5

    .line 142
    nop

    .line 143
    int-to-float v5, v2

    mul-float/2addr v5, v6

    invoke-static {v5}, Ljava/lang/Math;->round(F)I

    move-result v5

    invoke-static {v3, v5}, Ljava/lang/Math;->max(II)I

    move-result v5

    .line 144
    int-to-float v7, v4

    mul-float/2addr v7, v6

    invoke-static {v7}, Ljava/lang/Math;->round(F)I

    move-result v6

    invoke-static {v3, v6}, Ljava/lang/Math;->max(II)I

    move-result v6

    .line 142
    invoke-static {v0, v5, v6, v3}, Landroid/graphics/Bitmap;->createScaledBitmap(Landroid/graphics/Bitmap;IIZ)Landroid/graphics/Bitmap;

    move-result-object v5

    .line 145
    if-eq v5, v0, :cond_2

    invoke-virtual {v0}, Landroid/graphics/Bitmap;->recycle()V

    .line 147
    :cond_2
    invoke-virtual {v5}, Landroid/graphics/Bitmap;->isMutable()Z

    move-result v6

    if-eqz v6, :cond_3

    move-object v0, v5

    goto :goto_0

    :cond_3
    sget-object v6, Landroid/graphics/Bitmap$Config;->ARGB_8888:Landroid/graphics/Bitmap$Config;

    invoke-virtual {v5, v6, v3}, Landroid/graphics/Bitmap;->copy(Landroid/graphics/Bitmap$Config;Z)Landroid/graphics/Bitmap;

    move-result-object v0

    .line 148
    :goto_0
    if-eq v0, v5, :cond_4

    invoke-virtual {v5}, Landroid/graphics/Bitmap;->recycle()V

    goto :goto_1

    .line 161
    :catchall_0
    move-exception v2

    goto :goto_2

    .line 150
    :cond_4
    :goto_1
    iget-object v5, p0, Lapp/capri/ArenaScreen$Api30$Capture;->bounds:Ljava/util/ArrayList;

    invoke-static {v0, v5, v2, v4}, Lapp/capri/ArenaScreen;->access$0(Landroid/graphics/Bitmap;Ljava/util/ArrayList;II)V

    .line 151
    invoke-static {v0}, Lapp/capri/ArenaScreen;->access$1(Landroid/graphics/Bitmap;)[B

    move-result-object v2

    .line 152
    iget-object v4, p0, Lapp/capri/ArenaScreen$Api30$Capture;->finished:Ljava/util/concurrent/atomic/AtomicBoolean;

    const/4 v5, 0x0

    invoke-virtual {v4, v5, v3}, Ljava/util/concurrent/atomic/AtomicBoolean;->compareAndSet(ZZ)Z

    move-result v3
    :try_end_3
    .catchall {:try_start_3 .. :try_end_3} :catchall_0

    if-nez v3, :cond_7

    .line 164
    if-eqz v0, :cond_5

    if-eq v0, p1, :cond_5

    invoke-virtual {v0}, Landroid/graphics/Bitmap;->recycle()V

    .line 165
    :cond_5
    invoke-virtual {p1}, Landroid/graphics/Bitmap;->recycle()V

    .line 166
    if-eqz v1, :cond_6

    invoke-virtual {v1}, Landroid/hardware/HardwareBuffer;->close()V

    .line 152
    :cond_6
    return-void

    .line 153
    :cond_7
    :try_start_4
    invoke-static {v2}, Lapp/capri/ArenaScreen;->access$2([B)Ljava/lang/String;

    move-result-object v2

    .line 154
    invoke-static {}, Lapp/capri/ArenaScreen;->access$3()Landroid/os/Handler;

    move-result-object v3

    new-instance v4, Lapp/capri/ArenaScreen$Api30$Capture$1;

    invoke-direct {v4, p0, v2}, Lapp/capri/ArenaScreen$Api30$Capture$1;-><init>(Lapp/capri/ArenaScreen$Api30$Capture;Ljava/lang/String;)V

    invoke-virtual {v3, v4}, Landroid/os/Handler;->post(Ljava/lang/Runnable;)Z
    :try_end_4
    .catchall {:try_start_4 .. :try_end_4} :catchall_0

    .line 161
    nop

    .line 164
    if-eqz v0, :cond_8

    if-eq v0, p1, :cond_8

    invoke-virtual {v0}, Landroid/graphics/Bitmap;->recycle()V

    .line 165
    :cond_8
    invoke-virtual {p1}, Landroid/graphics/Bitmap;->recycle()V

    .line 166
    if-eqz v1, :cond_d

    goto :goto_3

    .line 136
    :cond_9
    :try_start_5
    new-instance v2, Ljava/lang/IllegalStateException;

    const-string v3, "no bitmap copy"

    invoke-direct {v2, v3}, Ljava/lang/IllegalStateException;-><init>(Ljava/lang/String;)V

    throw v2

    .line 134
    :cond_a
    new-instance v2, Ljava/lang/IllegalStateException;

    const-string v3, "no bitmap"

    invoke-direct {v2, v3}, Ljava/lang/IllegalStateException;-><init>(Ljava/lang/String;)V

    throw v2
    :try_end_5
    .catchall {:try_start_5 .. :try_end_5} :catchall_0

    .line 161
    :catchall_1
    move-exception p1

    .line 162
    move-object p1, v0

    goto :goto_2

    .line 161
    :catchall_2
    move-exception p1

    move-object p1, v0

    move-object v1, p1

    .line 162
    :goto_2
    :try_start_6
    const-string v2, "Screenshot unavailable: could not prepare the image."

    invoke-virtual {p0, v2}, Lapp/capri/ArenaScreen$Api30$Capture;->fail(Ljava/lang/String;)V
    :try_end_6
    .catchall {:try_start_6 .. :try_end_6} :catchall_3

    .line 164
    if-eqz v0, :cond_b

    if-eq v0, p1, :cond_b

    invoke-virtual {v0}, Landroid/graphics/Bitmap;->recycle()V

    .line 165
    :cond_b
    if-eqz p1, :cond_c

    invoke-virtual {p1}, Landroid/graphics/Bitmap;->recycle()V

    .line 166
    :cond_c
    if-eqz v1, :cond_d

    :goto_3
    invoke-virtual {v1}, Landroid/hardware/HardwareBuffer;->close()V

    .line 168
    :cond_d
    return-void

    .line 163
    :catchall_3
    move-exception v2

    .line 164
    if-eqz v0, :cond_e

    if-eq v0, p1, :cond_e

    invoke-virtual {v0}, Landroid/graphics/Bitmap;->recycle()V

    .line 165
    :cond_e
    if-eqz p1, :cond_f

    invoke-virtual {p1}, Landroid/graphics/Bitmap;->recycle()V

    .line 166
    :cond_f
    if-eqz v1, :cond_10

    invoke-virtual {v1}, Landroid/hardware/HardwareBuffer;->close()V

    .line 167
    :cond_10
    throw v2
.end method
