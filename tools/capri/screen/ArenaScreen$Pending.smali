.class final Lapp/capri/ArenaScreen$Pending;
.super Ljava/lang/Object;
.source "ArenaScreen.java"


# annotations
.annotation system Ldalvik/annotation/EnclosingClass;
    value = Lapp/capri/ArenaScreen;
.end annotation

.annotation system Ldalvik/annotation/InnerClass;
    accessFlags = 0x1a
    name = "Pending"
.end annotation


# instance fields
.field final expires:J

.field final jpeg:[B


# direct methods
.method constructor <init>([B)V
    .locals 4

    .line 55
    invoke-direct {p0}, Ljava/lang/Object;-><init>()V

    .line 56
    iput-object p1, p0, Lapp/capri/ArenaScreen$Pending;->jpeg:[B

    .line 57
    invoke-static {}, Landroid/os/SystemClock;->elapsedRealtime()J

    move-result-wide v0

    const-wide/32 v2, 0x1d4c0

    add-long/2addr v0, v2

    iput-wide v0, p0, Lapp/capri/ArenaScreen$Pending;->expires:J

    .line 58
    return-void
.end method
