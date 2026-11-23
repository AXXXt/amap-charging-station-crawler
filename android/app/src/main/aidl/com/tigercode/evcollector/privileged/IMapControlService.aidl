package com.tigercode.evcollector.privileged;

import android.os.Bundle;

interface IMapControlService {
    void destroy() = 16777114;
    Bundle forceStopMap() = 1;
}
